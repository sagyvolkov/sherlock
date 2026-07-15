"""
handlers/ycsb.py — SherlockSuite handler.

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
from src.parsers.benchmarks import parse_ycsb
from src.utils.credentials import resolve as resolve_credentials

logger = logging.getLogger(__name__)

GROUP   = "sherlock.io"
VERSION = "v1alpha1"
KIND = "SherlockYCSBSuite"

DEFAULT_CREDENTIALS = {
    "username": "sherlock",
    "password": "sherlock",
    "dbName":   "sherlock",
}


@kopf.on.create(GROUP, VERSION, KIND)
def on_create(name, namespace, spec, patch, **kwargs):
    logger.info(f"[ycsb] Suite {name} created in {namespace}")
    initialize_suite(name, namespace, spec, patch, suite_kind=KIND)
    secret_name = spec.get("database", {}).get("credentialsSecret")
    db_type = spec.get("database", {}).get("type", "ycsb")
    credentials = resolve_credentials(db_type, namespace, secret_name)
    deploy_db_infrastructure(name, namespace, spec, patch, credentials)
    patch.status["phase"] = "Ready"


@kopf.on.field(GROUP, VERSION, KIND, field='status.phase')
def on_phase_change(name, namespace, spec, status, patch, old, new, **kwargs):
    if new == "Ready":
        run_next_benchmark(
            suite_name=name, namespace=namespace, spec=spec, status=status,
            patch=patch, build_benchmark_command=_build_command,
            parse_results=parse_ycsb, benchmark_image=BENCHMARK_IMAGE,
        )


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
        succeeded=complete, parse_results=parse_ycsb,
    )


@kopf.on.delete(GROUP, VERSION, KIND)
def on_delete(name, namespace, spec, status, **kwargs):
    logger.info(f"[ycsb] Suite {name} deleted")


def _build_command(params: dict, db_ip: str) -> list[str]:
    """
    Map YCSB sweep params to run_ycsb script args.
    run_ycsb <db_type> <job_type> <db_ip> <workload> <threads>
             <username> <password> <dbname> <recordcount> <opcount>
             <distribution> <readprop> <updateprop> <runtime>
    """
    workload  = params.get("workload", "a")
    threads   = params.get("threads", 4)
    records   = params.get("recordCount", 1000000)
    ops       = params.get("operationCount", 100000)
    dist      = params.get("requestDistribution", "uniform")
    runtime   = params.get("maxExecutionTime", 120)
    creds     = DEFAULT_CREDENTIALS
    # Default read/update proportions per YCSB workload definition
    WORKLOAD_PROPS = {
        "a": ("0.5", "0.5"), "b": ("0.95", "0.05"), "c": ("1.0", "0.0"),
        "d": ("0.95", "0.0"), "e": ("0.95", "0.0"), "f": ("0.5", "0.0"),
    }
    read_prop, update_prop = WORKLOAD_PROPS.get(workload, ("0.5", "0.5"))
    cmd = (
        f"./run_ycsb mongodb run {db_ip} {workload} {threads} "
        f"{creds['username']} {creds['password']} {creds['dbName']} "
        f"{records} {ops} {dist} {read_prop} {update_prop} {runtime}"
    )
    return ["bash", "-c", cmd]
