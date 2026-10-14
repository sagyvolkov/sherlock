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
    run_next_benchmark,
    BENCHMARK_IMAGE,
    on_benchmark_job_event, on_suite_timer,
)
from src.parsers.benchmarks import parse_ycsb
from src.utils.credentials import resolve as resolve_credentials

logger = logging.getLogger(__name__)

GROUP   = "sherlock.io"
VERSION = "v1alpha1"
KIND = "SherlockYCSBSuite"
PLURAL  = "sherlockycsbbenchsuites"

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
def on_phase_change(name, namespace, spec, status, patch, old, new, body, **kwargs):
    if new == "Ready":
        run_next_benchmark(
            suite_name=name, namespace=namespace, spec=spec, status=status,
            patch=patch, build_benchmark_command=_build_command,
            parse_results=parse_ycsb, benchmark_image=BENCHMARK_IMAGE,
            suite_kind=KIND,
            owner=body,
        )


@kopf.on.event('batch', 'v1', 'jobs',
               labels={f'{GROUP}/suite-kind': KIND})
def on_job_event(type, name, namespace, status, meta, **kwargs):
    """A benchmark Job changed; finish its run if it was the last one."""
    on_benchmark_job_event(
        event_type=type, job_name=name, namespace=namespace,
        job_meta=meta, job_status=status,
        plural=PLURAL, parse_results=parse_ycsb,
    )


@kopf.timer(GROUP, VERSION, KIND, interval=10.0,
            when=lambda status, **_: (status or {}).get("phase") in ("Running", "Sleeping"))
def on_timer(name, namespace, status, patch, **kwargs):
    """Wake up after sleepBetweenRuns; back-stop for missed Job events."""
    on_suite_timer(name, namespace, status, patch,
                   plural=PLURAL, parse_results=parse_ycsb)


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
