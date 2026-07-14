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
    initialize_suite, deploy_db_infrastructure,
    run_next_benchmark, handle_run_completion,
    BENCHMARK_IMAGE,
)
from src.parsers.benchmarks import parse_pgbench

logger = logging.getLogger(__name__)

GROUP   = "sherlock.io"
VERSION = "v1alpha1"
KIND    = "SherlockPgbenchSuite"

# Default DB credentials — overridable via a Secret reference in future
DEFAULT_CREDENTIALS = {
    "username": "sherlock",
    "password": "sherlock",
    "dbName":   "sherlock",
}


# ── Create ────────────────────────────────────────────────────────────────────

@kopf.on.create(GROUP, VERSION, KIND)
def on_create(name, namespace, spec, patch, **kwargs):
    """
    Called when a new SherlockPgbenchSuite CR is created.
    Expands the sweep matrix and starts deploying database pods.
    """
    logger.info(f"[pgbench] Suite {name} created in {namespace}")
    initialize_suite(name, namespace, spec, patch, suite_kind=KIND)
    _deploy(name, namespace, spec, patch)


# ── Phase watcher: Ready → Running ────────────────────────────────────────────

@kopf.on.field(GROUP, VERSION, KIND, field='status.phase')
def on_phase_change(name, namespace, spec, status, patch, old, new, **kwargs):
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
        )


# ── Job completion watcher ────────────────────────────────────────────────────

@kopf.on.event('batch', 'v1', 'jobs',
               labels={f'{GROUP}/suite-kind': KIND})
def on_job_event(name, namespace, spec, status, meta, patch, **kwargs):
    """
    Watch all Jobs that belong to a pgbench Suite run.
    On completion or failure, trigger result collection and advance to next run.
    """
    conditions = (status or {}).get("conditions", [])
    suite_name = meta.get("labels", {}).get("sherlock.io/suite-name", "")
    run_name   = meta.get("labels", {}).get("sherlock.io/run-name", "")
    role       = meta.get("labels", {}).get("sherlock.io/role", "")

    if role != "benchmark":
        return  # ignore stat collector jobs here

    complete = any(c.get("type") == "Complete" and c.get("status") == "True"
                   for c in conditions)
    failed   = any(c.get("type") == "Failed"   and c.get("status") == "True"
                   for c in conditions)

    if not (complete or failed):
        return  # still running

    # Load the parent Suite CR to get current status
    suite = kopf.get(GROUP, VERSION, KIND, name=suite_name, namespace=namespace)
    if not suite:
        return

    handle_run_completion(
        suite_name=suite_name,
        namespace=namespace,
        spec=suite.get("spec", {}),
        status=suite.get("status", {}),
        patch=patch,
        run_name=run_name,
        job_name=name,
        succeeded=complete,
        parse_results=parse_pgbench,
    )


# ── Delete ────────────────────────────────────────────────────────────────────

@kopf.on.delete(GROUP, VERSION, KIND)
def on_delete(name, namespace, spec, status, **kwargs):
    """
    Clean up database pods when the Suite CR is deleted.
    PVCs are only deleted if artifacts.retention.deleteWithSuite=true.
    """
    db_spec    = spec.get("database", {})
    delete_pvcs = (
        spec.get("artifacts", {})
        .get("retention", {})
        .get("deleteWithSuite", False)
    )
    total = db_spec.get("instancesPerNode", 1) * len(
        status.get("deployments", [])
    )
    if total > 0:
        from src.db.deploy import teardown_databases
        teardown_databases(
            suite_name=name,
            namespace=namespace,
            db_type=db_spec.get("type", "postgresql"),
            total_instances=total,
            delete_pvcs=delete_pvcs,
        )
    logger.info(f"[pgbench] Suite {name} cleaned up")


# ── Private helpers ───────────────────────────────────────────────────────────

def _deploy(name, namespace, spec, patch):
    """Deploy PostgreSQL instances and transition to Ready."""
    deploy_db_infrastructure(
        name=name,
        namespace=namespace,
        spec=spec,
        patch=patch,
        db_credentials=DEFAULT_CREDENTIALS,
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
    clients   = params.get("clients", 2)
    threads   = params.get("threads", 4)
    duration  = params.get("duration", 120)
    rw_ratio  = params.get("readWriteRatio", 100)
    scale     = params.get("scaleFactor", 100)
    protocol  = params.get("protocol", "simple")

    # Map readWriteRatio to pgbench flags
    # 100 = read-only (-S), 0 = write-only, other = custom script
    read_only = "true" if rw_ratio == 100 else "false"

    command = (
        f"./run_pgbench run {db_ip} {clients} {threads} "
        f"time {duration} false false {scale} "
        f"{DEFAULT_CREDENTIALS['username']} "
        f"{DEFAULT_CREDENTIALS['password']} "
        f"{DEFAULT_CREDENTIALS['dbName']} "
        f"10 {read_only}"
    )

    return ["bash", "-c", command]
