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
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import kopf
from kubernetes.client.rest import ApiException

from src.utils.matrix import expand, make_run_name, count_runs, varying_keys
from src.utils import k8s, volumes
from src.utils.artifacts import (
    build_bundle, store_bundle, enforce_retention, parse_pvc_bundle_path,
)
from src.db.deploy import deploy_databases, wait_for_databases_ready, teardown_databases
from src.plugins.storage.generic import (
    launch_stat_collectors, stats_count, stats_settings, wait_for_collectors_running,
)
from src.parsers.base import parse_storage_stats
from src.parsers.aggregate import aggregate_results
from src.utils.credentials import write_suite_secret, client_secret_env

logger = logging.getLogger(__name__)

# ── Sherlock Suite labels ─────────────────────────────────────────────────────
LABEL_SUITE      = "sherlock.io/suite-name"
LABEL_KIND       = "sherlock.io/suite-kind"
LABEL_RUN        = "sherlock.io/run-name"
LABEL_ROLE       = "sherlock.io/role"

GROUP   = "sherlock.io"
VERSION = "v1alpha1"

# Benchmark container image (shared across all DB benchmarks in v1)
# Individual handlers can override this.
BENCHMARK_IMAGE = "quay.io/sagyvolkov/benchmark-container:sherlock0.5"

# Max time to wait for data preparation Jobs (e.g. pgbench -i) before a run
PREPARE_TIMEOUT_SECS = 3600


# ── Phase transitions ─────────────────────────────────────────────────────────


class _SuitePatch:
    """
    Accumulates status writes and flushes them directly to a suite's
    status subresource via CustomObjectsApi.

    Used by on_job_event handlers where kopf's `patch` belongs to the
    watched Job object, not the suite CR.
    """
    def __init__(self, plural: str):
        self._status = {}
        self._plural = plural

    @property
    def status(self):
        return self._status

    def flush(self, suite_name: str, namespace: str):
        from src.utils import k8s as _k8s
        if self._status:
            _k8s.patch_suite_status(
                GROUP, VERSION, self._plural,
                suite_name, namespace, self._status,
            )

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
    swept = varying_keys(matrix)

    run_entries = [
        {
            "name":        make_run_name(name, params, first_keys=swept),
            "params":      params,
            "phase":       "Pending",
            "retryCount":  0,
        }
        for params in matrix
    ]
    names = [r["name"] for r in run_entries]
    if len(set(names)) != len(names):
        # Runs are tracked by name; duplicates would complete each other
        raise kopf.PermanentError(f"Duplicate run names in the sweep matrix: {names}")

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
    owner: dict,
    suite_kind: str,
) -> list[dict]:
    """
    Phase: Deploying → Ready

    Deploys database pods and waits for them to be ready.
    Returns the deployment descriptors with ClusterIPs resolved.
    Reuses the PVCs (and the data on them) of an earlier suite when the
    layout matches; status.volumes records which PVCs and whether they were
    reused. Waits (retries) while a running suite is using the same volumes.

    owner: the suite CR (kopf `body`); it owns the Deployments and Services.
    Must be called after initialize_suite, whose runMatrix it reads.

    Replaces create_databases + wait logic.
    """
    # Store credentials in status (non-sensitive fields only — password excluded)
    # Handlers access username/dbName from status; password passed via Secret ref
    patch.status["dbCredentials"] = {
        "username": db_credentials.get("username", ""),
        "dbName":   db_credentials.get("dbName", ""),
        # password intentionally not stored in status — handlers re-resolve from Secret
    }
    db_spec = spec["database"]
    # Database pods and benchmark Jobs read passwords only from this Secret
    secret_name = write_suite_secret(name, namespace, db_spec["type"],
                                     db_credentials, owner)
    vol = volumes.settings(db_spec, default_prefix=db_spec["type"])
    run_matrix = patch.status.get("runMatrix") or []
    first_params = run_matrix[0]["params"] if run_matrix else {}
    worker_nodes = k8s.get_worker_nodes(
        node_selector=db_spec.get("nodeSelector"),
        count=None,  # all matching nodes
    )

    if not worker_nodes:
        raise kopf.TemporaryError("No worker nodes found", delay=30)

    try:
        deployments, volume_status = deploy_databases(
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
            owner=owner,
            suite_kind=suite_kind,
            name_prefix=vol["namePrefix"],
            on_create=vol["onCreate"],
            dataset=volumes.dataset_params(suite_kind, first_params),
            sa_password=db_credentials.get("saPassword"),
            credentials_secret=secret_name,
        )
    except volumes.VolumesBusy as e:
        logger.info(f"[{name}] Waiting: {e}")
        raise kopf.TemporaryError(str(e), delay=60)

    deployments = wait_for_databases_ready(deployments, namespace)
    patch.status["phase"] = "Ready"
    patch.status["deployments"] = deployments
    patch.status["volumes"] = volume_status

    logger.info(f"[{name}] {len(deployments)} database instances ready")
    return deployments


def teardown_db_suite(name: str, namespace: str, spec: dict, status: dict, uid: str):
    """
    Shared body of every database suite's kopf.on.delete handler.

    Deployments and Services go with the suite (ownerReferences). PVCs follow
    spec.database.volumes.onSuiteDelete: retain (default) keeps them and their
    prepared data for the next suite, delete removes those this suite claims.
    """
    db_spec = spec.get("database", {})
    vol = volumes.settings(db_spec, default_prefix=db_spec.get("type", ""))
    teardown_databases(
        namespace=namespace,
        owner_uid=uid,
        volume_status=(status or {}).get("volumes") or {},
        on_suite_delete=vol["onSuiteDelete"],
    )
    logger.info(f"[{name}] Suite deleted (volumes: {vol['onSuiteDelete']})")


def run_next_benchmark(
    suite_name: str,
    namespace: str,
    spec: dict,
    status: dict,
    patch: kopf.Patch,
    build_benchmark_command: Callable[[dict, str], list[str]],
    parse_results: Callable[[str, dict], dict],
    benchmark_image: str = BENCHMARK_IMAGE,
    suite_kind: str = "",
    build_prepare_command: Optional[
        Callable[[dict, str], list[str]]
    ] = None,
    owner: Optional[dict] = None,
):
    """
    Phase: Ready → Running

    Finds the next Pending run in the matrix and launches benchmark Jobs for it.
    Replaces the main loop in run_database_workload-parallel.

    build_benchmark_command(params, db_ip) → list of strings (the container command)
      params also carries _dbType, _username, _dbName and _clientCpus.
      Passwords are not passed: the Jobs get them as environment variables
      from the suite's credentials Secret (credentials.CLIENT_ENV), and the
      commands refer to those variables.
    Prepare and benchmark Jobs use execution.clientResources (see
    client_resources).
    parse_results(raw_log, params) → dict of result metrics
    build_prepare_command(params, db_ip) → container command that loads the
      benchmark data (the "prepare" job type in run_database_workload-parallel).
      It is skipped when the PVCs' prepared-data markers show they already
      hold data for this run's layout and dataset params (utils/volumes.py),
      whether that data came from an earlier run or an earlier suite.
    owner → the suite CR (kopf `body`). Every Job gets an ownerReference to
      it, so Jobs are deleted with the suite and stale Jobs from an earlier
      suite with the same name can be told apart. Its UID also goes into
      the Job names, which keeps them unique and under 63 characters.
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
    params   = _command_params(spec, status, next_run["params"])
    secret_env = client_secret_env(suite_name, db_spec.get("type", "postgresql"))
    suite_uid = ((owner or {}).get("metadata") or {}).get("uid", "")
    # Unique per suite instance, run and retry attempt
    run_key  = f"{suite_uid}/{run_name}/{next_run.get('retryCount', 0)}"
    patch.status["phase"] = "Running"
    patch.status["summary"] = {**status.get("summary", {}), "currentRunName": run_name}

    # Mark this run as Running in the matrix
    _update_run_phase(patch, run_matrix, run_name, "Running",
                      start_time=_now())

    # Get deployments from status (set during Deploying phase)
    deployments = status.get("deployments", [])
    worker_nodes = list({d["node"] for d in deployments})

    # ── Prepare benchmark data ────────────────────────────────────────────────
    # Blocks until done, before the stat collectors start, so that stats
    # cover only the measured run.
    if build_prepare_command is not None:
        prepared = _prepare_databases(
            suite_name=suite_name,
            namespace=namespace,
            spec=spec,
            run_name=run_name,
            deployments=deployments,
            params=params,
            volume_status=status.get("volumes") or {},
            build_prepare_command=build_prepare_command,
            benchmark_image=benchmark_image,
            suite_kind=suite_kind,
            owner=owner,
            run_key=run_key,
            secret_env=secret_env,
        )
        if not prepared:
            _update_run_phase(patch, run_matrix, run_name, "Failed",
                              end_time=_now())
            patch.status["phase"] = "Failed"
            return

    # ── Launch stat collectors ────────────────────────────────────────────────
    stat_jobs = {}
    if spec.get("artifacts", {}).get("collectStats", True):
        interval, margin = stats_settings(spec.get("artifacts"))
        stat_jobs = launch_stat_collectors(
            suite_name=suite_name,
            run_name=run_name,
            namespace=namespace,
            worker_nodes=worker_nodes,
            stats_interval=interval,
            stats_count=stats_count(_get_runtime_secs(params, spec), interval, margin),
            owner=owner,
            name_key=run_key,
        )
        wait_for_collectors_running(stat_jobs, namespace)

    # ── Launch benchmark jobs ─────────────────────────────────────────────────
    timeout_secs = k8s.parse_duration_secs(execution.get("timeoutPerRun"), 3600)
    benchmark_jobs = {}
    for dep in deployments:
        db_ip = dep.get("ip", "")
        # Was f"{dep['name']}-{run_name}"[:63], which collided for runs that
        # only differ in parameters past the cut-off
        job_name = k8s.job_name(f"{dep['name']}-run", f"{run_key}/bench/{dep['name']}")

        command = build_benchmark_command(params, db_ip)

        labels = {
            LABEL_SUITE: suite_name,
            LABEL_KIND:  suite_kind,
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
            resources=client_resources(spec),
            owner=owner,
            # A hung benchmark is marked Failed instead of blocking the run
            active_deadline_seconds=timeout_secs,
            secret_env=secret_env,
        )
        benchmark_jobs[job_name] = dep

    # Store job refs in status so the event watcher can find them
    _update_run_phase(
        patch, run_matrix, run_name, "Running",
        extra={"benchmarkJobs": list(benchmark_jobs.keys()),
               "statJobs": list(stat_jobs.keys()), "statNodes": stat_jobs}
    )

    logger.info(
        f"[{suite_name}] Run {run_name} started: "
        f"{len(benchmark_jobs)} benchmark jobs, {len(stat_jobs)} stat collectors"
    )


# ── Run completion ────────────────────────────────────────────────────────────
#
# A run has one benchmark Job per DB instance (or per PVC for fio). The run is
# finished only when every one of them is terminal, and it must be finished
# exactly once even though each Job produces its own event and the suite timer
# also checks. complete_run_if_done() is the single entry point for both:
#
#   1. Re-read the suite and confirm the run is still the current one.
#   2. Return early unless every benchmark Job is terminal.
#   3. Claim the run by patching suite phase Running → Collecting with a
#      resourceVersion precondition. Only one caller can win; the others get
#      409, re-read, see Collecting, and stop.
#   4. The winner parses all Job logs together and advances the suite.

CLAIM_ATTEMPTS = 5


def on_benchmark_job_event(
    event_type: Optional[str],
    job_name: str,
    namespace: str,
    job_meta: dict,
    job_status: dict,
    plural: str,
    parse_results: Callable[[str, dict], dict],
):
    """
    Shared body of every suite's kopf.on.event handler for benchmark Jobs.

    Ignores deletions, non-benchmark Jobs, Jobs still running, and Jobs not
    owned by the current suite (e.g. left over from a deleted suite with the
    same name). Everything else is handed to complete_run_if_done().
    """
    if event_type == "DELETED":
        return
    labels = job_meta.get("labels") or {}
    if labels.get(LABEL_ROLE) != "benchmark":
        return
    conditions = (job_status or {}).get("conditions") or []
    if k8s.job_state_from_conditions(conditions) == "Active":
        return

    owner_uids = {o.get("uid") for o in job_meta.get("ownerReferences") or []}
    if not owner_uids:
        logger.debug(f"Ignoring Job {job_name}: no ownerReferences (pre-v2 Job)")
        return

    complete_run_if_done(
        plural=plural,
        suite_name=labels.get(LABEL_SUITE, ""),
        namespace=namespace,
        parse_results=parse_results,
        run_name=labels.get(LABEL_RUN, ""),
        job_name=job_name,
        job_owner_uids=owner_uids,
    )


def on_suite_timer(
    name: str,
    namespace: str,
    status: dict,
    patch: kopf.Patch,
    plural: str,
    parse_results: Callable[[str, dict], dict],
):
    """
    Shared body of every suite's kopf.timer handler.

    Sleeping: once status.nextRunAt has passed, set phase Ready, which the
      status.phase field handler turns into the next run. This replaces the
      time.sleep() that used to block the Job event handler.
    Running: check whether the current run has finished. Job events normally
      get there first; this catches events missed while the operator was
      restarting or before the run's Job list was written to status.
    """
    phase = (status or {}).get("phase")
    if phase == "Sleeping":
        next_run_at = status.get("nextRunAt")
        if not next_run_at or _parse_time(next_run_at) <= _utcnow():
            patch.status["phase"] = "Ready"
            patch.status["nextRunAt"] = None
    elif phase == "Running":
        complete_run_if_done(
            plural=plural,
            suite_name=name,
            namespace=namespace,
            parse_results=parse_results,
        )


def complete_run_if_done(
    plural: str,
    suite_name: str,
    namespace: str,
    parse_results: Callable[[str, dict], dict],
    run_name: Optional[str] = None,
    job_name: Optional[str] = None,
    job_owner_uids: Optional[set] = None,
) -> bool:
    """
    Finish the suite's current run if all its benchmark Jobs are terminal.

    run_name / job_name / job_owner_uids come from a Job event and restrict
    the check to that Job's run; the timer passes none of them and checks
    whichever run is Running. Returns True if this call finished the run.
    """
    for _ in range(CLAIM_ATTEMPTS):
        suite = k8s.get_custom_resource(GROUP, VERSION, plural, suite_name, namespace)
        if not suite:
            return False
        meta   = suite.get("metadata", {})
        spec   = suite.get("spec", {})
        status = suite.get("status") or {}

        if job_owner_uids is not None and meta.get("uid") not in job_owner_uids:
            logger.info(
                f"[{suite_name}] Ignoring Job {job_name}: owned by a different "
                f"suite instance (stale Job from a deleted suite)"
            )
            return False
        if status.get("phase") != "Running":
            return False

        run_matrix = status.get("runMatrix", [])
        run = next(
            (r for r in run_matrix
             if r.get("phase") == "Running"
             and (run_name is None or r.get("name") == run_name)),
            None,
        )
        if run is None:
            return False
        job_names = run.get("benchmarkJobs") or []
        if not job_names:
            return False  # Jobs not recorded yet; the timer will retry
        if job_name is not None and job_name not in job_names:
            logger.info(
                f"[{suite_name}] Ignoring Job {job_name}: not one of "
                f"run {run['name']}'s benchmark Jobs"
            )
            return False

        states = {j: k8s.get_job_state(j, namespace) for j in job_names}
        active = [j for j, s in states.items() if s == "Active"]
        if active:
            _log_progress(suite_name, run["name"],
                          len(job_names) - len(active), len(job_names))
            return False
        # Stat collectors print their data when their window ends, which is
        # after the benchmark; wait for them (the suite timer re-checks)
        stats_active = [j for j in run.get("statJobs") or []
                        if k8s.get_job_state(j, namespace) == "Active"]
        if stats_active:
            logger.debug(f"[{suite_name}] Run {run['name']}: waiting for "
                         f"{len(stats_active)} stat collectors")
            return False

        try:
            k8s.patch_suite_status(
                GROUP, VERSION, plural, suite_name, namespace,
                {"phase": "Collecting"},
                resource_version=meta.get("resourceVersion"),
            )
        except ApiException as e:
            if e.status == 409:
                continue  # someone else changed the suite; re-read and recheck
            raise

        # This caller owns the run from here on.
        _last_progress.pop((suite_name, run["name"]), None)
        patch = _SuitePatch(plural=plural)
        try:
            handle_run_completion(
                suite_name=suite_name, namespace=namespace,
                spec=spec, status=status, patch=patch,
                run=run, job_states=states, parse_results=parse_results,
                owner=suite,
            )
        except Exception:
            logger.exception(
                f"[{suite_name}] Error while completing run {run['name']}"
            )
            patch = _SuitePatch(plural=plural)
            _update_run_phase(patch, run_matrix, run["name"], "Failed",
                              end_time=_now())
            patch.status["phase"] = "Failed"
        patch.flush(suite_name, namespace)
        return True

    logger.warning(
        f"[{suite_name}] Could not claim run completion after "
        f"{CLAIM_ATTEMPTS} attempts; the suite timer will retry"
    )
    return False


# Last "N/M benchmark Jobs done" count logged per (suite, run), so the timer
# only logs at INFO when the count changes instead of every 10s.
_last_progress: dict = {}


def _log_progress(suite_name: str, run_name: str, done: int, total: int):
    key = (suite_name, run_name)
    level = logging.DEBUG if _last_progress.get(key) == done else logging.INFO
    _last_progress[key] = done
    logger.log(level, f"[{suite_name}] Run {run_name}: {done}/{total} benchmark Jobs done")


def handle_run_completion(
    suite_name: str,
    namespace: str,
    spec: dict,
    status: dict,
    patch: "_SuitePatch",
    run: dict,
    job_states: dict[str, str],
    parse_results: Callable[[str, dict], dict],
    owner: Optional[dict] = None,
):
    """
    Called exactly once per run attempt, after every benchmark Job is terminal
    and the run has been claimed (suite phase is Collecting).

    Parses all Job logs, combines them into one result, stores the artifact
    bundle, applies the failure policy, and moves the suite on.

    Replaces the log-collection and result-printing logic in
    run_database_workload-parallel.
    """
    run_name   = run["name"]
    params     = run.get("params", {})
    run_matrix = status.get("runMatrix", [])

    benchmark_logs = {
        j: (k8s.get_job_pod_logs(j, namespace) if state != "Missing" else "")
        for j, state in job_states.items()
    }

    # A Job that failed, vanished, or "succeeded" without printing any metrics
    # (run_pgbench exits 0 even when pgbench fails) all count as failures.
    problems = {
        j: f"Job {state}" for j, state in job_states.items() if state != "Complete"
    }
    instance_results = []
    for j in job_states:
        if j in problems:
            continue
        parsed = parse_results(benchmark_logs[j], params)
        if not parsed:
            problems[j] = "no benchmark metrics in output"
            continue
        instance_results.append({"job": j, **parsed})

    if problems:
        for j, reason in problems.items():
            logger.error(
                f"[{suite_name}] Run {run_name}: {j}: {reason}\n"
                f"{benchmark_logs.get(j, '')[-2000:]}"
            )
        _handle_run_failure(suite_name, spec, status, patch, run)
        return

    bench_results = aggregate_results(
        [{k: v for k, v in r.items() if k != "job"} for r in instance_results]
    )

    # Collect stat logs and parse storage metrics
    artifacts_spec = spec.get("artifacts", {})
    stat_job_names = run.get("statJobs", [])
    stat_logs = {j: k8s.get_job_pod_logs(j, namespace) for j in stat_job_names}
    storage_results = {}
    if stat_logs:
        stat_nodes = run.get("statNodes") or {}
        storage_results = parse_storage_stats(
            {stat_nodes.get(j, j): log for j, log in stat_logs.items()},
            runtime_secs=_get_runtime_secs(params, spec),
            interval=stats_settings(artifacts_spec)[0],
            device_pattern=artifacts_spec.get("statsDevicePattern"),
            network_interfaces=artifacts_spec.get("statsNetworkInterfaces"),
        )

    result = {**bench_results, **storage_results}

    # ── Build and store artifact bundle ───────────────────────────────────────
    try:
        bundle = build_bundle(
            run_name=run_name,
            benchmark_logs=benchmark_logs,
            stat_logs=stat_logs,
            parsed_result={**result, "instances": instance_results},
            suite_spec=spec,
            params=params,
            compression=artifacts_spec.get("compression", "gzip"),
        )
        record = store_bundle(
            bundle=bundle,
            run_name=run_name,
            suite_name=suite_name,
            namespace=namespace,
            artifacts_spec=artifacts_spec,
            owner=owner,
        )
        artifact_path = record["path"]
        # Earlier runs' bundles come from status: this patch starts empty
        bundles = enforce_retention(
            suite_name, artifacts_spec,
            (status.get("artifacts") or {}).get("bundles", []) + [record],
            namespace=namespace, owner=owner,
        )
        patch.status["artifacts"] = {**(status.get("artifacts") or {}), "bundles": bundles}
        if artifact_path.startswith("pvc://"):
            patch.status["artifacts"]["pvcName"] = parse_pvc_bundle_path(artifact_path)[0]
    except Exception as e:
        logger.warning(f"[{suite_name}] Artifact collection failed for {run_name}: {e}")
        artifact_path = ""

    _update_run_phase(
        patch, run_matrix, run_name, "Completed",
        end_time=_now(),
        extra={
            "result": result,
            "instanceResults": instance_results,
            "artifactPath": artifact_path,
        },
    )
    _increment_summary(patch, status, completed=True)

    logger.info(
        f"[{suite_name}] Run {run_name} completed "
        f"({len(instance_results)} instances). "
        f"Primary metric: {_primary_metric(result)}"
    )
    _advance(suite_name, spec, status, patch)


def _handle_run_failure(suite_name, spec, status, patch, run):
    """Apply execution.failurePolicy to a failed run."""
    run_name   = run["name"]
    run_matrix = status.get("runMatrix", [])
    execution  = spec.get("execution", {})
    failure_policy = execution.get("failurePolicy", "abortAll")
    retry_count = run.get("retryCount", 0)
    retry_limit = execution.get("retryLimit", 3)

    if failure_policy == "retryN" and retry_count < retry_limit:
        logger.warning(
            f"[{suite_name}] Run {run_name} failed "
            f"(attempt {retry_count + 1}/{retry_limit}), retrying"
        )
        # Back to Pending; the retry gets fresh Job names (attempt is hashed in)
        _update_run_phase(
            patch, run_matrix, run_name, "Pending",
            extra={"retryCount": retry_count + 1,
                   "benchmarkJobs": [], "statJobs": []},
        )
        _advance(suite_name, spec, status, patch)
        return

    logger.error(f"[{suite_name}] Run {run_name} failed")
    _update_run_phase(patch, run_matrix, run_name, "Failed", end_time=_now())
    _increment_summary(patch, status, failed=True)

    if failure_policy == "abortAll":
        patch.status["phase"] = "Failed"
        logger.error(f"[{suite_name}] Aborting suite due to failed run")
        return

    # continueAndReport: keep going, suite ends Degraded
    _advance(suite_name, spec, status, patch)


def _advance(suite_name, spec, status, patch):
    """
    After a run finishes: sleep before the next run, start it now, or finish.

    Sleeping is a persisted phase with a wake-up time (status.nextRunAt);
    the suite timer moves it to Ready. Nothing blocks a handler thread.
    """
    run_matrix = patch.status.get("runMatrix") or status.get("runMatrix", [])
    if not any(r["phase"] == "Pending" for r in run_matrix):
        _finalize_suite(suite_name, None, spec, status, patch)
        return

    sleep_secs = spec.get("execution", {}).get("sleepBetweenRuns", 30)
    if sleep_secs > 0:
        wake_at = _utcnow() + timedelta(seconds=sleep_secs)
        patch.status["phase"] = "Sleeping"
        patch.status["nextRunAt"] = wake_at.isoformat()
        logger.info(f"[{suite_name}] Sleeping {sleep_secs}s before next run")
    else:
        patch.status["phase"] = "Ready"


# ── Private helpers ───────────────────────────────────────────────────────────

def _command_params(spec: dict, status: dict, params: dict) -> dict:
    """Run params plus the non-secret connection details for building commands."""
    db_creds = (status or {}).get("dbCredentials") or {}
    return {
        **params,
        "_dbType":   (spec.get("database") or {}).get("type", "postgresql"),
        "_username": db_creds.get("username", ""),
        "_dbName":   db_creds.get("dbName", ""),
        # Whole CPUs the client pod may use; sizes parallel data loading
        "_clientCpus": client_cpus(spec),
    }


def client_resources(spec: dict) -> dict:
    """
    Resources for the benchmark client pods (prepare and run Jobs):
    execution.clientResources, or database.resources if it is not set.
    """
    return ((spec.get("execution") or {}).get("clientResources")
            or (spec.get("database") or {}).get("resources") or {})


def client_cpus(spec: dict) -> int:
    """
    Whole CPUs a client pod may use: limits.cpu, else requests.cpu, else
    the Job default (k8s.create_job). "500m" counts as 1.
    """
    res = client_resources(spec)
    cpu = ((res.get("limits") or {}).get("cpu")
           or (res.get("requests") or {}).get("cpu")
           or k8s.DEFAULT_JOB_CPU_LIMIT)
    cpu = str(cpu)
    value = float(cpu[:-1]) / 1000 if cpu.endswith("m") else float(cpu)
    return max(1, int(value))


def _prepare_databases(
    suite_name: str,
    namespace: str,
    spec: dict,
    run_name: str,
    deployments: list[dict],
    params: dict,
    volume_status: dict,
    build_prepare_command: Callable[[dict, str], list[str]],
    benchmark_image: str,
    suite_kind: str,
    owner: Optional[dict] = None,
    run_key: str = "",
    secret_env: Optional[dict] = None,
) -> bool:
    """
    Launch one prepare Job per database instance and block until all finish.
    Returns False if any prepare Job failed or timed out.

    Skipped when every PVC's marker already matches this run's layout. The
    markers are cleared before the Jobs start and written once all succeed,
    so an interrupted or failed prepare never leaves a valid-looking marker.
    Suites without status.volumes (created before volume reuse) always
    prepare and never mark.

    Replaces the `-j prepare` invocation of run_database_workload-parallel.
    """
    pvc_names = [d["pvc"] for d in deployments if d.get("pvc")]
    layout = None
    if volume_status.get("layout"):
        layout = {**volume_status["layout"],
                  "dataset": volumes.dataset_params(suite_kind, params)}
        if volumes.is_prepared(namespace, pvc_names, layout):
            logger.info(
                f"[{suite_name}] Skipping prepare for {run_name}: volumes "
                f"already hold {layout['dataset']}"
            )
            return True
        volumes.clear_marker(namespace, pvc_names)

    prepare_jobs = []
    for dep in deployments:
        command = build_prepare_command(params, dep.get("ip", ""))
        # Unique per suite instance, run and attempt, so a leftover Job is
        # never picked up by create_job's 409 path
        job_name = k8s.job_name(
            f"{dep['name']}-prep", f"{run_key or run_name}/prep/{dep['name']}"
        )
        k8s.create_job(
            name=job_name,
            namespace=namespace,
            image=benchmark_image,
            command=command,
            node_name=dep["node"],
            labels={
                LABEL_SUITE: suite_name,
                LABEL_KIND:  suite_kind,
                LABEL_RUN:   run_name,
                LABEL_ROLE:  "prepare",
            },
            resources=client_resources(spec),
            owner=owner,
            active_deadline_seconds=PREPARE_TIMEOUT_SECS,
            secret_env=secret_env,
        )
        prepare_jobs.append(job_name)

    logger.info(
        f"[{suite_name}] Preparing data for {run_name}: {len(prepare_jobs)} jobs"
    )
    pending = set(prepare_jobs)
    deadline = time.time() + PREPARE_TIMEOUT_SECS
    while pending:
        for job_name in sorted(pending):
            state = k8s.get_job_state(job_name, namespace)
            if state in ("Failed", "Missing"):
                logger.error(
                    f"[{suite_name}] Prepare job {job_name} {state.lower()}:\n"
                    f"{k8s.get_job_pod_logs(job_name, namespace)[-2000:]}"
                )
                return False
            if state == "Complete":
                pending.discard(job_name)
        if not pending:
            break
        if time.time() > deadline:
            logger.error(
                f"[{suite_name}] Prepare jobs timed out after "
                f"{PREPARE_TIMEOUT_SECS}s: {sorted(pending)}"
            )
            return False
        time.sleep(5)

    if layout is not None:
        volumes.mark_prepared(namespace, pvc_names, layout)
    logger.info(f"[{suite_name}] Data prepared for {run_name}")
    return True


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
    """
    Update a single run entry in the runMatrix status.

    Starts from the runMatrix already in the patch, if any, so that several
    calls in one handler accumulate instead of each starting over from the
    original status (which used to drop startTime).
    """
    updated = []
    for run in (patch.status.get("runMatrix") or run_matrix):
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
                # HammerDB measures after the ramp-up; collect stats for both
                return (int(val) + int(params.get("rampupMins", 0))) * 60
            return int(val)
    return 120  # fallback


def _primary_metric(result: dict) -> str:
    """Return a human-readable string of the primary metric for logging."""
    for key in ("tps", "nopm", "throughputOpsPerSec", "readIops", "totalIops"):
        if key in result:
            return f"{key}={result[key]}"
    return str(result)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _utcnow().isoformat()


def _parse_time(value: str) -> datetime:
    """Parse an ISO timestamp as written by _now() (or with a trailing Z)."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
