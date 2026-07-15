"""
handlers/base.py — Shared reconcile logic for all Sherlock Suite handlers.

All five benchmark Suite handlers (pgbench, hammerdb, sysbench, ycsb, fio)
follow the same state machine. This base module contains the shared steps;
each benchmark handler adds only the benchmark-specific parts
(command construction, result parsing).

State machine:
  Pending → Deploying → Ready → Running → Sleeping → Running → ... → Collecting → Completed
                                    ↓ (on failure)
                                  Failed / Degraded (continueAndReport)

This replaces the orchestration flow in:
  - create_databases (Deploying phase)
  - run_database_workload-parallel (Running phase)
  - run_loops (the sequential loop with sleep)
  - print_results (Collecting phase)
"""

import logging
import time
from typing import Callable, Optional

import kopf

from src.utils.matrix import expand, make_run_name, count_runs
from src.utils import k8s
from src.utils.artifacts import build_bundle, store_bundle, enforce_retention
from src.db.deploy import deploy_databases, wait_for_databases_ready, teardown_databases
from src.plugins.storage.generic import (
    launch_stat_collectors, collect_stat_logs
)
from src.parsers.base import parse_storage_stats

logger = logging.getLogger(__name__)

# ── Sherlock Suite labels ─────────────────────────────────────────────────────
LABEL_SUITE      = "sherlock.io/suite-name"
LABEL_KIND       = "sherlock.io/suite-kind"
LABEL_RUN        = "sherlock.io/run-name"
LABEL_ROLE       = "sherlock.io/role"

# Benchmark container image (shared across all DB benchmarks in v1)
# Individual handlers can override this.
BENCHMARK_IMAGE = "quay.io/sagyvolkov/benchmark-container:sherlock0.5"


# ── Phase transitions ─────────────────────────────────────────────────────────

def initialize_suite(
    name: str,
    namespace: str,
    spec: dict,
    patch: kopf.Patch,
    suite_kind: str,
    is_fio: bool = False,
) -> list[dict]:
    """
    Phase: Pending → Deploying (DB benchmarks) or Provisioning (fio)

    Expands the sweep matrix and writes it to status.runMatrix.
    This is called from the on.create handler of each Suite kind.

    Returns the expanded run matrix (list of param dicts).
    """
    sweep = spec.get("sweep", {})
    matrix = expand(sweep)

    run_entries = [
        {
            "name":        make_run_name(name, params),
            "params":      params,
            "phase":       "Pending",
            "retryCount":  0,
        }
        for params in matrix
    ]

    patch.status["phase"]     = "Provisioning" if is_fio else "Deploying"
    patch.status["runMatrix"] = run_entries
    patch.status["summary"]   = {
        "totalRuns":      len(run_entries),
        "runsCompleted":  0,
        "runsFailed":     0,
        "runsRemaining":  len(run_entries),
        "currentRunName": "",
    }

    logger.info(
        f"[{name}] Initialized suite: {len(run_entries)} runs "
        f"({count_runs(sweep)} combinations)"
    )
    return run_entries


def deploy_db_infrastructure(
    name: str,
    namespace: str,
    spec: dict,
    patch: kopf.Patch,
    db_credentials: dict,
) -> list[dict]:
    # Store credentials in status (non-sensitive fields only — password excluded)
    # Handlers access username/dbName from status; password passed via Secret ref
    patch.status["dbCredentials"] = {
        "username": db_credentials.get("username", ""),
        "dbName":   db_credentials.get("dbName", ""),
        # password intentionally not stored in status — handlers re-resolve from Secret
    }
    """
    Phase: Deploying → Ready

    Deploys database pods and waits for them to be ready.
    Returns the deployment descriptors with ClusterIPs resolved.

    Replaces create_databases + wait logic.
    """
    db_spec = spec["database"]
    worker_nodes = k8s.get_worker_nodes(
        node_selector=db_spec.get("nodeSelector"),
        count=None,  # all matching nodes
    )

    if not worker_nodes:
        raise kopf.TemporaryError("No worker nodes found", delay=30)

    deployments = deploy_databases(
        suite_name=name,
        namespace=namespace,
        db_type=db_spec["type"],
        db_username=db_credentials["username"],
        db_password=db_credentials["password"],
        db_name=db_credentials["dbName"],
        storage_class=db_spec.get("storageClass", ""),
        storage_size=db_spec["storageSize"],
        instances_per_node=db_spec.get("instancesPerNode", 1),
        worker_nodes=worker_nodes,
        resources=db_spec.get("resources", {}),
        sa_password=db_credentials.get("saPassword"),
    )

    deployments = wait_for_databases_ready(deployments, namespace)
    patch.status["phase"] = "Ready"
    patch.status["deployments"] = deployments

    logger.info(f"[{name}] {len(deployments)} database instances ready")
    return deployments


def run_next_benchmark(
    suite_name: str,
    namespace: str,
    spec: dict,
    status: dict,
    patch: kopf.Patch,
    build_benchmark_command: Callable[[dict, str], list[str]],
    parse_results: Callable[[str, dict], dict],
    benchmark_image: str = BENCHMARK_IMAGE,
):
    """
    Phase: Ready/Sleeping → Running

    Finds the next Pending run in the matrix and launches benchmark Jobs for it.
    Replaces the main loop in run_database_workload-parallel.

    build_benchmark_command(params, db_ip) → list of strings (the container command)
    parse_results(raw_log, params) → dict of result metrics
    """
    run_matrix = status.get("runMatrix", [])
    execution  = spec.get("execution", {})
    db_spec    = spec.get("database", {})

    # Find next pending run
    next_run = next((r for r in run_matrix if r["phase"] == "Pending"), None)
    if next_run is None:
        # All runs done — move to Collecting
        _finalize_suite(suite_name, namespace, spec, status, patch)
        return

    run_name = next_run["name"]
    params   = next_run["params"]
    patch.status["phase"] = "Running"
    patch.status["summary"]["currentRunName"] = run_name

    # Mark this run as Running in the matrix
    _update_run_phase(patch, run_matrix, run_name, "Running",
                      start_time=_now())

    # Get deployments from status (set during Deploying phase)
    deployments = status.get("deployments", [])
    worker_nodes = list({d["node"] for d in deployments})

    # ── Launch stat collectors ────────────────────────────────────────────────
    stat_jobs = {}
    if spec.get("artifacts", {}).get("collectStats", True):
        runtime_secs = _get_runtime_secs(params, spec)
        stats_interval = 10
        stats_count = (runtime_secs + 10) // stats_interval

        stat_jobs = launch_stat_collectors(
            suite_name=suite_name,
            run_name=run_name,
            namespace=namespace,
            worker_nodes=worker_nodes,
            stats_interval=stats_interval,
            stats_count=stats_count,
        )

    # ── Launch benchmark jobs ─────────────────────────────────────────────────
    benchmark_jobs = {}
    for dep in deployments:
        db_ip = dep.get("ip", "")
        job_name = f"{dep['name']}-{run_name}"
        # k8s name length limit
        if len(job_name) > 63:
            job_name = job_name[:63]

        command = build_benchmark_command(params, db_ip)

        labels = {
            LABEL_SUITE: suite_name,
            LABEL_RUN:   run_name,
            LABEL_ROLE:  "benchmark",
        }

        k8s.create_job(
            name=job_name,
            namespace=namespace,
            image=benchmark_image,
            command=command,
            node_name=dep["node"],
            labels=labels,
            resources=spec.get("database", {}).get("resources", {}),
        )
        benchmark_jobs[job_name] = dep

    # Store job refs in status so the event watcher can find them
    _update_run_phase(
        patch, run_matrix, run_name, "Running",
        extra={"benchmarkJobs": list(benchmark_jobs.keys()),
               "statJobs": list(stat_jobs.keys())}
    )

    logger.info(
        f"[{suite_name}] Run {run_name} started: "
        f"{len(benchmark_jobs)} benchmark jobs, {len(stat_jobs)} stat collectors"
    )


def handle_run_completion(
    suite_name: str,
    namespace: str,
    spec: dict,
    status: dict,
    patch: kopf.Patch,
    run_name: str,
    job_name: str,
    succeeded: bool,
    parse_results: Callable[[str, dict], dict],
):
    """
    Called when a benchmark job completes (success or failure).
    Collects logs, parses results, applies failure policy.

    Replaces the log-collection and result-printing logic in
    run_database_workload-parallel.
    """
    run_matrix   = status.get("runMatrix", [])
    current_run  = next((r for r in run_matrix if r["name"] == run_name), None)
    execution    = spec.get("execution", {})
    failure_policy = execution.get("failurePolicy", "abortAll")

    if not succeeded:
        retry_count = (current_run or {}).get("retryCount", 0)
        retry_limit = execution.get("retryLimit", 3)

        if failure_policy == "retryN" and retry_count < retry_limit:
            logger.warning(
                f"[{suite_name}] Run {run_name} failed "
                f"(attempt {retry_count + 1}/{retry_limit}), retrying"
            )
            _update_run_phase(
                patch, run_matrix, run_name, "Pending",
                extra={"retryCount": retry_count + 1}
            )
            return

        logger.error(f"[{suite_name}] Run {run_name} failed")
        _update_run_phase(patch, run_matrix, run_name, "Failed",
                          end_time=_now())
        _increment_summary(patch, status, failed=True)

        if failure_policy == "abortAll":
            patch.status["phase"] = "Failed"
            logger.error(f"[{suite_name}] Aborting suite due to failed run")
            return

        # continueAndReport — mark suite Degraded at end, keep going
        _start_next_or_finish(
            suite_name, namespace, spec, status, patch, parse_results
        )
        return

    # ── Success: collect logs and parse results ───────────────────────────────
    benchmark_log = k8s.get_job_pod_logs(job_name, namespace)
    params = (current_run or {}).get("params", {})
    bench_results = parse_results(benchmark_log, params)

    # Collect stat logs and parse storage metrics
    stat_job_names = (current_run or {}).get("statJobs", [])
    storage_results = {}
    if stat_job_names:
        stat_logs = collect_stat_logs(
            {j: j for j in stat_job_names}, namespace
        )
        # Merge storage metrics across all nodes (average)
        all_iostat = "\n".join(stat_logs.values())
        storage_results = parse_storage_stats(all_iostat, "")

    result = {**bench_results, **storage_results}

    # ── Build and store artifact bundle ───────────────────────────────────────
    artifacts_spec = spec.get("artifacts", {})
    patch.status["phase"] = "Collecting"
    try:
        bundle = build_bundle(
            run_name=run_name,
            benchmark_logs={job_name: benchmark_log},
            stat_logs={j: k8s.get_job_pod_logs(j, namespace)
                       for j in (current_run or {}).get("statJobs", [])},
            parsed_result=result,
            suite_spec=spec,
            params=params,
            compression=artifacts_spec.get("compression", "gzip"),
        )
        artifact_path = store_bundle(
            bundle=bundle,
            run_name=run_name,
            suite_name=suite_name,
            namespace=namespace,
            artifacts_spec=artifacts_spec,
            patch=patch,
        )
        # Enforce retention policy
        current_bundles = (
            patch.status.get("artifacts", {}).get("bundles", [])
            or status.get("artifacts", {}).get("bundles", [])
        )
        enforce_retention(suite_name, artifacts_spec, current_bundles)

    except Exception as e:
        logger.warning(f"[{suite_name}] Artifact collection failed for {run_name}: {e}")
        artifact_path = ""

    _update_run_phase(
        patch, run_matrix, run_name, "Completed",
        end_time=_now(),
        extra={"result": result, "artifactPath": artifact_path}
    )
    _increment_summary(patch, status, completed=True)

    logger.info(
        f"[{suite_name}] Run {run_name} completed. "
        f"Primary metric: {_primary_metric(result)}"
    )

    # Sleep between runs if sequential
    sleep_secs = spec.get("execution", {}).get("sleepBetweenRuns", 30)
    if sleep_secs > 0:
        patch.status["phase"] = "Sleeping"
        logger.info(f"[{suite_name}] Sleeping {sleep_secs}s before next run")
        time.sleep(sleep_secs)

    _start_next_or_finish(suite_name, namespace, spec, status, patch, parse_results)


# ── Private helpers ───────────────────────────────────────────────────────────

def _start_next_or_finish(
    suite_name, namespace, spec, status, patch, parse_results
):
    """Check if there are more runs; if not, finalize."""
    run_matrix = patch.status.get("runMatrix") or status.get("runMatrix", [])
    remaining = [r for r in run_matrix if r["phase"] == "Pending"]
    if remaining:
        patch.status["phase"] = "Ready"  # triggers next run via phase watcher
    else:
        _finalize_suite(suite_name, namespace, spec, status, patch)


def _finalize_suite(suite_name, namespace, spec, status, patch):
    """Mark the suite as Completed or Degraded."""
    run_matrix = patch.status.get("runMatrix") or status.get("runMatrix", [])
    failed = [r for r in run_matrix if r["phase"] == "Failed"]
    if failed:
        patch.status["phase"] = "Degraded"
        logger.warning(
            f"[{suite_name}] Suite completed with {len(failed)} failed runs → Degraded"
        )
    else:
        patch.status["phase"] = "Completed"
        logger.info(f"[{suite_name}] Suite completed successfully")


def _update_run_phase(
    patch: kopf.Patch,
    run_matrix: list,
    run_name: str,
    phase: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    extra: Optional[dict] = None,
):
    """Update a single run entry in the runMatrix status."""
    updated = []
    for run in run_matrix:
        if run["name"] == run_name:
            run = {**run, "phase": phase}
            if start_time: run["startTime"]      = start_time
            if end_time:   run["completionTime"] = end_time
            if extra:      run.update(extra)
        updated.append(run)
    patch.status["runMatrix"] = updated


def _increment_summary(
    patch: kopf.Patch,
    status: dict,
    completed: bool = False,
    failed: bool = False,
):
    """Increment run counters in status.summary."""
    summary = dict(status.get("summary", {}))
    if completed:
        summary["runsCompleted"]  = summary.get("runsCompleted", 0) + 1
    if failed:
        summary["runsFailed"]     = summary.get("runsFailed", 0) + 1
    remaining = (
        summary.get("totalRuns", 0)
        - summary.get("runsCompleted", 0)
        - summary.get("runsFailed", 0)
    )
    summary["runsRemaining"] = max(0, remaining)
    patch.status["summary"] = summary


def _get_runtime_secs(params: dict, spec: dict) -> int:
    """Extract the benchmark runtime in seconds from run params."""
    # Each benchmark uses a different param name for duration
    for key in ("duration", "testMins", "runtime", "maxExecutionTime"):
        if key in params:
            val = params[key]
            if key == "testMins":
                return int(val) * 60
            return int(val)
    return 120  # fallback


def _primary_metric(result: dict) -> str:
    """Return a human-readable string of the primary metric for logging."""
    for key in ("tps", "nopm", "throughputOpsPerSec", "readIops", "totalIops"):
        if key in result:
            return f"{key}={result[key]}"
    return str(result)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
