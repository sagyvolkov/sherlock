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
    run_next_benchmark, handle_run_completion,
    BENCHMARK_IMAGE,
)
from src.parsers.benchmarks import parse_hammerdb

logger = logging.getLogger(__name__)

GROUP   = "sherlock.io"
VERSION = "v1alpha1"
KIND = "SherlockHammerDBSuite"

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
    deploy_db_infrastructure(name, namespace, spec, patch, DEFAULT_CREDENTIALS)
    patch.status["phase"] = "Ready"


@kopf.on.field(GROUP, VERSION, KIND, field='status.phase')
def on_phase_change(name, namespace, spec, status, patch, old, new, **kwargs):
    if new == "Ready":
        run_next_benchmark(
            suite_name=name, namespace=namespace, spec=spec, status=status,
            patch=patch, build_benchmark_command=_build_command,
            parse_results=parse_hammerdb, benchmark_image=BENCHMARK_IMAGE,
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
        succeeded=complete, parse_results=parse_hammerdb,
    )


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
