"""
handlers/fio.py — SherlockSuite handler.

Follows the same pattern as handlers/pgbench.py.
Benchmark-specific: command construction and result parsing.
All orchestration is in handlers/base.py.
"""

import logging
import kopf

from src.handlers.base import (
    initialize_suite, deploy_db_infrastructure,
    run_next_benchmark, handle_run_completion,
    BENCHMARK_IMAGE,
)
from src.parsers.benchmarks import parse_fio

logger = logging.getLogger(__name__)

GROUP   = "sherlock.io"
VERSION = "v1alpha1"
KIND = "SherlockFioSuite"

FIO_IMAGE = "quay.io/sagyvolkov/fio_container:0.5"


@kopf.on.create(GROUP, VERSION, KIND)
def on_create(name, namespace, spec, patch, **kwargs):
    """
    fio is storage-only — no database layer.
    Goes Pending → Provisioning (PVCs) → Ready → Running.
    """
    logger.info(f"[fio] Suite {name} created in {namespace}")
    from src.utils import k8s

    initialize_suite(name, namespace, spec, patch, suite_kind=KIND, is_fio=True)

    # Create PVCs for all workers × fioPerWorker
    storage = spec["storage"]
    worker_count    = storage.get("workerCount", 3)
    fio_per_worker  = storage.get("fioPerWorker", 1)
    storage_class   = storage.get("storageClass", "")
    storage_size    = storage["storageSize"]
    volume_mode     = storage.get("volumeMode", "Filesystem")
    node_selector   = storage.get("nodeSelector")

    worker_nodes = k8s.get_worker_nodes(node_selector=node_selector, count=worker_count)
    if not worker_nodes:
        raise kopf.TemporaryError("No worker nodes found", delay=30)

    k8s.ensure_namespace(namespace)
    total = fio_per_worker * len(worker_nodes)
    pvc_names = []
    for j in range(total):
        pvc_name = f"{name}-fio-pvc-{j}"
        k8s.create_pvc(
            name=pvc_name, namespace=namespace,
            storage_size=storage_size, storage_class=storage_class,
            volume_mode=volume_mode,
            labels={"sherlock.io/suite": name, "sherlock.io/role": "fio-pvc"},
        )
        pvc_names.append(pvc_name)

    k8s.wait_for_pvcs_bound(namespace, pvc_names, timeout=900)

    patch.status["phase"] = "Ready"
    patch.status["workerNodes"] = worker_nodes
    patch.status["pvcNames"] = pvc_names
    patch.status["summary"]["totalPvcsProvisioned"] = total


@kopf.on.field(GROUP, VERSION, KIND, field='status.phase')
def on_phase_change(name, namespace, spec, status, patch, old, new, **kwargs):
    if new != "Ready":
        return

    from src.utils import k8s
    from src.plugins.storage.generic import launch_stat_collectors
    from src.parsers.benchmarks import parse_fio

    run_matrix   = status.get("runMatrix", [])
    next_run     = next((r for r in run_matrix if r["phase"] == "Pending"), None)
    if not next_run:
        patch.status["phase"] = "Completed"
        return

    run_name     = next_run["name"]
    params       = next_run["params"]
    worker_nodes = status.get("workerNodes", [])
    pvc_names    = status.get("pvcNames", [])
    storage      = spec["storage"]
    volume_mode  = storage.get("volumeMode", "Filesystem")
    fio_per_worker = storage.get("fioPerWorker", 1)

    patch.status["phase"] = "Running"
    patch.status["summary"]["currentRunName"] = run_name

    # Launch stat collectors
    stat_jobs = {}
    if spec.get("artifacts", {}).get("collectStats", True):
        runtime = params.get("runtime", 60)
        stat_jobs = launch_stat_collectors(
            suite_name=name, run_name=run_name, namespace=namespace,
            worker_nodes=worker_nodes,
            stats_count=(runtime + 10) // 10,
        )

    # Launch one fio Job per PVC
    for j, pvc_name in enumerate(pvc_names):
        node_idx  = j % len(worker_nodes)
        node_name = worker_nodes[node_idx]
        job_name  = f"{name}-fio-{run_name}-{j}"[:63]
        command   = _build_fio_command(params, volume_mode, pvc_name)

        mount_path = "/data" if volume_mode == "Filesystem" else None
        pvc_mounts = (
            [{"pvc_name": pvc_name, "mount_path": "/data"}]
            if volume_mode == "Filesystem" else []
        )

        k8s.create_job(
            name=job_name, namespace=namespace,
            image=FIO_IMAGE, command=command,
            node_name=node_name,
            labels={
                "sherlock.io/suite-name": name,
                "sherlock.io/suite-kind": KIND,
                "sherlock.io/run-name":   run_name,
                "sherlock.io/role":       "benchmark",
            },
            resources=storage.get("resources", {}),
            pvc_mounts=pvc_mounts,
        )

    logger.info(f"[fio] Run {run_name} started on {len(pvc_names)} PVCs")


@kopf.on.event('batch', 'v1', 'jobs', labels={f'{GROUP}/suite-kind': KIND})
def on_job_event(name, namespace, spec, status, meta, patch, **kwargs):
    conditions = (status or {}).get("conditions", [])
    suite_name = meta.get("labels", {}).get("sherlock.io/suite-name", "")
    run_name   = meta.get("labels", {}).get("sherlock.io/run-name", "")
    if meta.get("labels", {}).get("sherlock.io/role") != "benchmark":
        return
    complete = any(c.get("type") == "Complete" and c.get("status") == "True" for c in conditions)
    failed   = any(c.get("type") == "Failed"   and c.get("status") == "True" for c in conditions)
    if not (complete or failed):
        return
    suite = kopf.get(GROUP, VERSION, KIND, name=suite_name, namespace=namespace)
    if not suite:
        return
    handle_run_completion(
        suite_name=suite_name, namespace=namespace,
        spec=suite.get("spec", {}), status=suite.get("status", {}),
        patch=patch, run_name=run_name, job_name=name,
        succeeded=complete, parse_results=parse_fio,
    )


@kopf.on.delete(GROUP, VERSION, KIND)
def on_delete(name, namespace, spec, status, **kwargs):
    """Clean up PVCs if deleteWithSuite=true."""
    delete_pvcs = (
        spec.get("artifacts", {}).get("retention", {}).get("deleteWithSuite", False)
    )
    if delete_pvcs:
        from src.utils import k8s
        for pvc_name in status.get("pvcNames", []):
            k8s.delete_pvc(pvc_name, namespace)
    logger.info(f"[fio] Suite {name} deleted")


def _build_fio_command(params: dict, volume_mode: str, pvc_name: str) -> list[str]:
    """
    Build fio container command from sweep parameters.
    Maps to: ./run_fio <rwmixwrite> <bs> <filename> <runtime> <jobs>
                       <iodepth> <worksize> <device_type> <direct> <iopattern>
    """
    bs        = params.get("blockSize", "4k")
    rw        = params.get("rwMixWrite", 0)
    pattern   = params.get("ioPattern", "randrw")
    runtime   = params.get("runtime", 60)
    jobs      = params.get("jobs", 4)
    iodepth   = params.get("ioDepth", 4)
    worksize  = params.get("workSize", "2048g")
    direct    = params.get("directIO", 1)
    dev_type  = "FS" if volume_mode == "Filesystem" else "RAW"
    filename  = "/data" if volume_mode == "Filesystem" else f"/dev/{pvc_name}"
    cmd = (
        f"./run_fio {rw} {bs} {filename} {runtime} "
        f"{jobs} {iodepth} {worksize} {dev_type} {direct} {pattern}"
    )
    return ["bash", "-c", cmd]
