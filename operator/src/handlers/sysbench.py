"""
handlers/sysbench.py — SherlockSuite handler.

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
from src.parsers.benchmarks import parse_sysbench
from src.utils.credentials import resolve as resolve_credentials

logger = logging.getLogger(__name__)

GROUP   = "sherlock.io"
VERSION = "v1alpha1"
KIND = "SherlockSysbenchSuite"

DEFAULT_CREDENTIALS = {
    "username": "sherlock",
    "password": "sherlock",
    "dbName":   "sherlock",
}


@kopf.on.create(GROUP, VERSION, KIND)
def on_create(name, namespace, spec, patch, **kwargs):
    logger.info(f"[sysbench] Suite {name} created in {namespace}")
    initialize_suite(name, namespace, spec, patch, suite_kind=KIND)
    secret_name = spec.get("database", {}).get("credentialsSecret")
    db_type = spec.get("database", {}).get("type", "sysbench")
    credentials = resolve_credentials(db_type, namespace, secret_name)
    deploy_db_infrastructure(name, namespace, spec, patch, credentials)
    patch.status["phase"] = "Ready"


@kopf.on.field(GROUP, VERSION, KIND, field='status.phase')
def on_phase_change(name, namespace, spec, status, patch, old, new, **kwargs):
    if new == "Ready":
        run_next_benchmark(
            suite_name=name, namespace=namespace, spec=spec, status=status,
            patch=patch, build_benchmark_command=_build_command,
            parse_results=parse_sysbench, benchmark_image=BENCHMARK_IMAGE,
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
        succeeded=complete, parse_results=parse_sysbench,
    )


@kopf.on.delete(GROUP, VERSION, KIND)
def on_delete(name, namespace, spec, status, **kwargs):
    logger.info(f"[sysbench] Suite {name} deleted")


def _build_command(params: dict, db_ip: str) -> list[str]:
    """
    Map sysbench sweep params to run_sysbench script args.
    run_sysbench <job_type> <runtime> <threads> <read_only> <write_only>
                 <db_ip> <output_interval> <rows> <tables> <db_type>
                 <username> <password> <dbname> <inserts> <updates> <non_idx_updates>
    """
    threads   = params.get("threads", 4)
    duration  = params.get("duration", 120)
    tables    = params.get("tables", 8)
    rows      = params.get("tableSize", 1000000)
    test_type = params.get("testType", "oltp_read_write")
    creds     = DEFAULT_CREDENTIALS
    read_only  = "on" if test_type == "oltp_read_only"  else "off"
    write_only = "on" if test_type == "oltp_write_only" else "off"
    cmd = (
        f"./run_sysbench run {duration} {threads} {read_only} {write_only} "
        f"{db_ip} 10 {rows} {tables} mysql "
        f"{creds['username']} {creds['password']} {creds['dbName']} 1 1 1"
    )
    return ["bash", "-c", cmd]
