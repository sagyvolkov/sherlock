"""
handlers/sysbench.py — SherlockSuite handler.

Follows the same pattern as handlers/pgbench.py.
Benchmark-specific: command construction and result parsing.
All orchestration is in handlers/base.py.
"""

import logging
import kopf

from src.handlers.base import (
    initialize_suite, deploy_db_infrastructure, teardown_db_suite,
    run_next_benchmark,
    BENCHMARK_IMAGE,
    on_benchmark_job_event, on_suite_timer,
)
from src.parsers.benchmarks import parse_sysbench
from src.utils.credentials import resolve as resolve_credentials

logger = logging.getLogger(__name__)

GROUP   = "sherlock.io"
VERSION = "v1alpha1"
KIND = "SherlockSysbenchSuite"
PLURAL  = "sherlocksysbenchsuites"

DEFAULT_CREDENTIALS = {
    "username": "sherlock",
    "password": "sherlock",
    "dbName":   "sherlock",
}


@kopf.on.create(GROUP, VERSION, KIND)
def on_create(name, namespace, spec, patch, body, **kwargs):
    logger.info(f"[sysbench] Suite {name} created in {namespace}")
    initialize_suite(name, namespace, spec, patch, suite_kind=KIND)
    secret_name = spec.get("database", {}).get("credentialsSecret")
    db_type = spec.get("database", {}).get("type", "sysbench")
    credentials = resolve_credentials(db_type, namespace, secret_name)
    deploy_db_infrastructure(name, namespace, spec, patch, credentials,
                             owner=body, suite_kind=KIND)
    patch.status["phase"] = "Ready"


@kopf.on.field(GROUP, VERSION, KIND, field='status.phase')
def on_phase_change(name, namespace, spec, status, patch, old, new, body, **kwargs):
    if new == "Ready":
        run_next_benchmark(
            suite_name=name, namespace=namespace, spec=spec, status=status,
            patch=patch, build_benchmark_command=_build_command,
            parse_results=parse_sysbench, benchmark_image=BENCHMARK_IMAGE,
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
        plural=PLURAL, parse_results=parse_sysbench,
    )


@kopf.timer(GROUP, VERSION, KIND, interval=10.0,
            when=lambda status, **_: (status or {}).get("phase") in ("Running", "Sleeping"))
def on_timer(name, namespace, status, patch, **kwargs):
    """Wake up after sleepBetweenRuns; back-stop for missed Job events."""
    on_suite_timer(name, namespace, status, patch,
                   plural=PLURAL, parse_results=parse_sysbench)


@kopf.on.delete(GROUP, VERSION, KIND)
def on_delete(name, namespace, spec, status, meta, **kwargs):
    """Database pods go with the suite; PVCs per database.volumes.onSuiteDelete."""
    teardown_db_suite(name, namespace, spec, status, meta["uid"])


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
