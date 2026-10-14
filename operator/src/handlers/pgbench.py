"""
handlers/pgbench.py — SherlockPgbenchSuite handler.

Handles lifecycle events for SherlockPgbenchSuite CRs.
The benchmark-specific logic is the command construction (run_pgbench args)
and result parsing. All orchestration is in handlers/base.py.

Replaces the pgbench sections of:
  - create_databases (database deployment)
  - run_database_workload-parallel -b pgbench (job launching)
  - print_results (log parsing)
"""

import logging
import kopf

from src.handlers.base import (
    initialize_suite, deploy_db_infrastructure, teardown_db_suite,
    run_next_benchmark,
    BENCHMARK_IMAGE,
    on_benchmark_job_event, on_suite_timer,
)
from src.parsers.benchmarks import parse_pgbench
from src.utils.credentials import resolve as resolve_credentials

logger = logging.getLogger(__name__)

GROUP   = "sherlock.io"
VERSION = "v1alpha1"
KIND    = "SherlockPgbenchSuite"
PLURAL  = "sherlockpgbenchsuites"


# ── Create ────────────────────────────────────────────────────────────────────

@kopf.on.create(GROUP, VERSION, KIND)
def on_create(name, namespace, spec, patch, body, **kwargs):
    """
    Called when a new SherlockPgbenchSuite CR is created.
    Expands the sweep matrix and starts deploying database pods.
    """
    logger.info(f"[pgbench] Suite {name} created in {namespace}")
    initialize_suite(name, namespace, spec, patch, suite_kind=KIND)
    _deploy(name, namespace, spec, patch, body)


# ── Phase watcher: Ready → Running ────────────────────────────────────────────

@kopf.on.field(GROUP, VERSION, KIND, field='status.phase')
def on_phase_change(name, namespace, spec, status, patch, old, new, body, **kwargs):
    """Drive state machine transitions."""
    logger.debug(f"[pgbench] {name} phase: {old} → {new}")

    if new == "Ready":
        # Start the next pending run
        run_next_benchmark(
            suite_name=name,
            namespace=namespace,
            spec=spec,
            status=status,
            patch=patch,
            build_benchmark_command=_build_command,
            parse_results=parse_pgbench,
            benchmark_image=BENCHMARK_IMAGE,
            suite_kind=KIND,
            owner=body,
            build_prepare_command=_build_prepare_command,
        )


# ── Job completion watcher ────────────────────────────────────────────────────

@kopf.on.event('batch', 'v1', 'jobs',
               labels={f'{GROUP}/suite-kind': KIND})
def on_job_event(type, name, namespace, status, meta, **kwargs):
    """A benchmark Job changed; finish its run if it was the last one."""
    on_benchmark_job_event(
        event_type=type, job_name=name, namespace=namespace,
        job_meta=meta, job_status=status,
        plural=PLURAL, parse_results=parse_pgbench,
    )


@kopf.timer(GROUP, VERSION, KIND, interval=10.0,
            when=lambda status, **_: (status or {}).get("phase") in ("Running", "Sleeping"))
def on_timer(name, namespace, status, patch, **kwargs):
    """Wake up after sleepBetweenRuns; back-stop for missed Job events."""
    on_suite_timer(name, namespace, status, patch,
                   plural=PLURAL, parse_results=parse_pgbench)


@kopf.on.delete(GROUP, VERSION, KIND)
def on_delete(name, namespace, spec, status, meta, **kwargs):
    """Database pods go with the suite; PVCs per database.volumes.onSuiteDelete."""
    teardown_db_suite(name, namespace, spec, status, meta["uid"])


# ── Private helpers ───────────────────────────────────────────────────────────

# readWriteRatio → run_pgbench workload type
_WORKLOAD_TYPES = {100: "readonly", 90: "90r10w", 70: "70r30w"}


def _deploy(name, namespace, spec, patch, body):
    """Deploy PostgreSQL instances and transition to Ready."""
    secret_name = spec.get("database", {}).get("credentialsSecret")
    credentials = resolve_credentials("postgresql", namespace, secret_name)
    deploy_db_infrastructure(
        name=name,
        namespace=namespace,
        spec=spec,
        patch=patch,
        db_credentials=credentials,
        owner=body,
        suite_kind=KIND,
    )
    patch.status["phase"] = "Ready"


def _build_command(params: dict, db_ip: str) -> list[str]:
    """
    Build the pgbench container command from sweep parameters.

    Maps CRD sweep fields to run_pgbench script arguments:
      run_pgbench <job_type> <db_ip> <clients> <threads> <run_type>
                  <run_type_var> <vacuum> <quiet> <scale> <username>
                  <password> <dbname> <output_interval> <read_only>

    Replaces the prepare_run_command() function for pgbench in
    run_database_workload-parallel.
    """
    # Map readWriteRatio to the run_pgbench workload type (its first argument):
    # 100 = readonly (-S), 90/70 = the bundled mixed scripts,
    # anything else falls through to pgbench's builtin TPC-B.
    rw_ratio = params.get("readWriteRatio", 100)
    action   = _WORKLOAD_TYPES.get(rw_ratio, "run")
    return ["bash", "-c", _run_pgbench(action, params, db_ip)]


def _build_prepare_command(params: dict, db_ip: str) -> list[str]:
    """
    Build the `run_pgbench prepare` command (pgbench -i) for a run.

    base skips it when the volumes already hold tables for this scaleFactor.
    """
    return ["bash", "-c", _run_pgbench("prepare", params, db_ip)]


def _run_pgbench(action: str, params: dict, db_ip: str) -> str:
    """Build the run_pgbench command line for the given action."""
    clients   = params.get("clients", 2)
    threads   = params.get("threads", 4)
    duration  = params.get("duration", 120)
    rw_ratio  = params.get("readWriteRatio", 100)
    scale     = params.get("scaleFactor", 100)

    read_only = "true" if rw_ratio == 100 else "false"

    # Credentials are passed in via params dict (populated from status.credentials
    # which is set during the deploy phase)
    username = params.get("_username", "sherlock")
    password = params.get("_password", "sherlock")
    db_name  = params.get("_dbName",   "sherlock")

    return (
        f"./run_pgbench {action} {db_ip} {clients} {threads} "
        f"time {duration} false false {scale} "
        f"{username} {password} {db_name} "
        f"10 {read_only}"
    )
