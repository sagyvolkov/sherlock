"""
handlers/sysbench.py — SherlockSuite handler.

Follows the same pattern as handlers/pgbench.py.
Benchmark-specific: command construction and result parsing.
All orchestration is in handlers/base.py.
"""

import logging
import shlex

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

# sysbench --db-driver and server port per database type
_DRIVERS = {"mysql": ("mysql", 3306), "postgresql": ("pgsql", 5432)}

# Threads used to load tables in parallel (sysbench loads one table per thread)
PREPARE_THREADS = 8


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
            build_prepare_command=_build_prepare_command,
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
    sysbench <testType> ... run

    Built here rather than through the image's run_sysbench script, which
    always runs oltp_read_write.lua on MySQL (testType was ignored) and
    passes --mysql-ignore-errors=all, hiding every SQL error. sysbench's
    default ignore list (deadlocks and lock wait timeouts) applies instead.
    """
    return _sysbench(params, db_ip, params.get("testType", "oltp_read_write"),
                     threads=params.get("threads", 4)) + [
        f"--time={params.get('duration', 120)}",
        f"--report-interval={params.get('reportInterval', 10)}",
        "--rand-type=uniform",
        "run",
    ]


def _build_prepare_command(params: dict, db_ip: str) -> list[str]:
    """
    Drop and recreate the sbtest tables for this run's tables / tableSize.

    base skips it when the volumes already hold this dataset. Always uses
    oltp_read_write, whose schema every oltp_* test (and bulk_insert) runs on.
    """
    threads = min(params.get("tables", 8), PREPARE_THREADS)
    sysbench = _sysbench(params, db_ip, "oltp_read_write", threads=threads)
    return ["bash", "-c",
            f"{shlex.join(sysbench + ['cleanup'])} && {shlex.join(sysbench + ['prepare'])}"]


def _sysbench(params: dict, db_ip: str, test: str, threads: int) -> list[str]:
    """sysbench <test> with connection and dataset options, no action."""
    driver, port = _DRIVERS[params.get("_dbType", "mysql")]
    return [
        "sysbench", test,
        f"--db-driver={driver}",
        f"--{driver}-host={db_ip}",
        f"--{driver}-port={port}",
        f"--{driver}-user={params['_username']}",
        f"--{driver}-password={params['_password']}",
        f"--{driver}-db={params['_dbName']}",
        f"--tables={params.get('tables', 8)}",
        f"--table-size={params.get('tableSize', 1000000)}",
        f"--threads={threads}",
    ]
