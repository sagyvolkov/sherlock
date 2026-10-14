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
    run_next_benchmark,
    _update_run_phase, _now,
    BENCHMARK_IMAGE,
    on_benchmark_job_event, on_suite_timer,
)
from src.parsers.benchmarks import parse_fio

logger = logging.getLogger(__name__)

GROUP   = "sherlock.io"
VERSION = "v1alpha1"
KIND = "SherlockFioSuite"
PLURAL = "sherlockfiosuites"

FIO_IMAGE = "quay.io/sagyvolkov/fio_container:0.6"


@kopf.on.create(GROUP, VERSION, KIND)
def on_create(name, namespace, spec, patch, body, **kwargs):
    """
    fio is storage-only — no database layer.
    Goes Pending → Provisioning (PVCs) → Ready → Running.

    PVCs are <namePrefix>-pvc-N (default prefix "fio") and are reused by a
    later suite with the same layout, like database volumes. fio has no
    prepare step, so the marker is written as soon as the PVCs are Bound.
    """
    logger.info(f"[fio] Suite {name} created in {namespace}")
    from src.utils import k8s, volumes

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
    vol = volumes.settings(storage, default_prefix="fio")
    layout = volumes.make_layout(
        suite_kind=KIND,
        total=total,
        instances_per_node=fio_per_worker,
        workers=len(worker_nodes),
        storage_class=storage_class,
        storage_size=storage_size,
        volume_mode=volume_mode,
    )
    try:
        pvc_names, reused = volumes.ensure_volume_set(
            prefix=vol["namePrefix"],
            namespace=namespace,
            layout=layout,
            owner=body,
            on_create=vol["onCreate"],
            labels={"sherlock.io/suite": name, "sherlock.io/role": "fio-pvc"},
            bound_timeout=900,
        )
    except volumes.VolumesBusy as e:
        logger.info(f"[fio] {name} waiting: {e}")
        raise kopf.TemporaryError(str(e), delay=60)
    if not reused:
        volumes.mark_prepared(namespace, pvc_names, layout)

    patch.status["phase"] = "Ready"
    patch.status["workerNodes"] = worker_nodes
    patch.status["pvcNames"] = pvc_names
    patch.status["volumes"] = {
        "namePrefix": vol["namePrefix"],
        "pvcNames":   pvc_names,
        "layout":     layout,
        "reused":     reused,
    }
    patch.status["summary"] = {"totalPvcsProvisioned": total}


@kopf.on.field(GROUP, VERSION, KIND, field='status.phase')
def on_phase_change(name, namespace, spec, status, patch, old, new, body, **kwargs):
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
    # Unique per suite instance, run and retry attempt (see base.run_next_benchmark)
    run_key      = f"{body['metadata']['uid']}/{run_name}/{next_run.get('retryCount', 0)}"
    worker_nodes = status.get("workerNodes", [])
    pvc_names    = status.get("pvcNames", [])
    storage      = spec["storage"]
    volume_mode  = storage.get("volumeMode", "Filesystem")
    fio_per_worker = storage.get("fioPerWorker", 1)

    patch.status["phase"] = "Running"
    # Build updated summary by merging with existing status.summary
    existing_summary = dict(status.get("summary", {}))
    existing_summary["currentRunName"] = run_name
    patch.status["summary"] = existing_summary

    # Mark this run as Running in the matrix (prevents re-entry on next phase event)
    _update_run_phase(patch, run_matrix, run_name, "Running", start_time=_now())

    # Launch stat collectors
    stat_jobs = {}
    if spec.get("artifacts", {}).get("collectStats", True):
        runtime = params.get("runtime", 60)
        stat_jobs = launch_stat_collectors(
            suite_name=name, run_name=run_name, namespace=namespace,
            worker_nodes=worker_nodes,
            stats_count=(runtime + 10) // 10,
            owner=body,
            name_key=run_key,
        )

    # Launch one fio Job per PVC
    timeout_secs = k8s.parse_duration_secs(
        spec.get("execution", {}).get("timeoutPerRun"), 3600
    )
    benchmark_jobs = []
    for j, pvc_name in enumerate(pvc_names):
        node_idx  = j % len(worker_nodes)
        node_name = worker_nodes[node_idx]
        # Was f"{name}-fio-{run_name}-{j}"[:63], which cut off the PVC index
        # for long run names so every PVC's Job got the same name
        job_name  = k8s.job_name(f"{name}-fio-{j}", f"{run_key}/fio/{j}")
        command   = _build_fio_command(params, volume_mode, pvc_name)

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
            owner=body,
            active_deadline_seconds=timeout_secs,
        )
        benchmark_jobs.append(job_name)

    # Store job refs in the run entry so the job event handler can match them
    _update_run_phase(
        patch, run_matrix, run_name, "Running",
        extra={"benchmarkJobs": benchmark_jobs,
               "statJobs": list(stat_jobs.keys())}
    )

    logger.info(f"[fio] Run {run_name} started on {len(pvc_names)} PVCs")


@kopf.on.event('batch', 'v1', 'jobs', labels={f'{GROUP}/suite-kind': KIND})
def on_job_event(type, name, namespace, status, meta, **kwargs):
    """A fio Job changed; finish its run if it was the last one."""
    on_benchmark_job_event(
        event_type=type, job_name=name, namespace=namespace,
        job_meta=meta, job_status=status,
        plural=PLURAL, parse_results=parse_fio,
    )


@kopf.timer(GROUP, VERSION, KIND, interval=10.0,
            when=lambda status, **_: (status or {}).get("phase") in ("Running", "Sleeping"))
def on_timer(name, namespace, status, patch, **kwargs):
    """Wake up after sleepBetweenRuns; back-stop for missed Job events."""
    on_suite_timer(name, namespace, status, patch,
                   plural=PLURAL, parse_results=parse_fio)


@kopf.on.delete(GROUP, VERSION, KIND)
def on_delete(name, namespace, spec, status, meta, **kwargs):
    """Keep the PVCs, or delete them if storage.volumes.onSuiteDelete=delete."""
    from src.utils import volumes
    vol = volumes.settings(spec.get("storage", {}), default_prefix="fio")
    prefix = ((status or {}).get("volumes") or {}).get("namePrefix")
    if prefix and vol["onSuiteDelete"] == "delete":
        deleted = volumes.delete_claimed(prefix, namespace, meta["uid"])
        logger.info(f"[fio] Deleted {len(deleted)} PVCs of volume set {prefix}")
    logger.info(f"[fio] Suite {name} deleted (volumes: {vol['onSuiteDelete']})")


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
    output_fmt = params.get("outputFormat", "json")
    return [
        "/scripts/run_fio",
        str(rw), str(bs), str(filename), str(runtime),
        str(jobs), str(iodepth), str(worksize), str(dev_type), str(direct), str(pattern),
        str(output_fmt),
    ]
