"""
handlers/hammerdb.py — SherlockSuite handler.

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
from src.parsers.benchmarks import parse_hammerdb
from src.utils.credentials import resolve as resolve_credentials

logger = logging.getLogger(__name__)

GROUP   = "sherlock.io"
VERSION = "v1alpha1"
KIND = "SherlockHammerDBSuite"
PLURAL  = "sherlockhammerdbsuites"

DEFAULT_CREDENTIALS = {
    "username": "sa",
    "password": "Sherlock1!",   # SA password; must meet MSSQL complexity requirements
    "dbName":   "tpcc",
    "saPassword": "Sherlock1!",
}


@kopf.on.create(GROUP, VERSION, KIND)
def on_create(name, namespace, spec, patch, **kwargs):
    logger.info(f"[hammerdb] Suite {name} created in {namespace}")
    initialize_suite(name, namespace, spec, patch, suite_kind=KIND)
    secret_name = spec.get("database", {}).get("credentialsSecret")
    db_type = spec.get("database", {}).get("type", "hammerdb")
    credentials = resolve_credentials(db_type, namespace, secret_name)
    deploy_db_infrastructure(name, namespace, spec, patch, credentials)
    patch.status["phase"] = "Ready"


@kopf.on.field(GROUP, VERSION, KIND, field='status.phase')
def on_phase_change(name, namespace, spec, status, patch, old, new, body, **kwargs):
    if new == "Ready":
        run_next_benchmark(
            suite_name=name, namespace=namespace, spec=spec, status=status,
            patch=patch, build_benchmark_command=_build_command,
            parse_results=parse_hammerdb, benchmark_image=BENCHMARK_IMAGE,
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
        plural=PLURAL, parse_results=parse_hammerdb,
    )


@kopf.timer(GROUP, VERSION, KIND, interval=10.0,
            when=lambda status, **_: (status or {}).get("phase") in ("Running", "Sleeping"))
def on_timer(name, namespace, status, patch, **kwargs):
    """Wake up after sleepBetweenRuns; back-stop for missed Job events."""
    on_suite_timer(name, namespace, status, patch,
                   plural=PLURAL, parse_results=parse_hammerdb)


@kopf.on.delete(GROUP, VERSION, KIND)
def on_delete(name, namespace, spec, status, **kwargs):
    logger.info(f"[hammerdb] Suite {name} deleted")


def _build_command(params: dict, db_ip: str) -> list[str]:
    """
    Map HammerDB sweep params to run_hammerdb script args.
    run_hammerdb mssqls run <db_ip> <sa_pass> <warehouses> <vusers>
                        <output_interval> <rampup_mins> <test_mins> <raise_error>
    """
    vu        = params.get("virtualUsers", 8)
    warehouses = params.get("warehouses", 100)
    rampup    = params.get("rampupMins", 2)
    test_mins = params.get("testMins", 5)
    sa_pass   = DEFAULT_CREDENTIALS["saPassword"]
    cmd = (
        f"./run_hammerdb mssqls run {db_ip} {sa_pass} "
        f"{warehouses} {vu} 10 {rampup} {test_mins} false"
    )
    return ["bash", "-c", cmd]
