"""
handlers/ycsb.py — SherlockSuite handler.

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
from src.parsers.benchmarks import parse_ycsb
from src.utils.credentials import resolve as resolve_credentials

logger = logging.getLogger(__name__)

GROUP   = "sherlock.io"
VERSION = "v1alpha1"
KIND = "SherlockYCSBSuite"
PLURAL  = "sherlockycsbbenchsuites"

# Read / update proportions of the image's workload files, passed again
# because run_ycsb always sets both (scans, inserts and read-modify-writes
# come from the file)
WORKLOAD_PROPS = {
    "a": ("0.5", "0.5"), "b": ("0.95", "0.05"), "c": ("1", "0"),
    "d": ("0.95", "0"), "e": ("0", "0"), "f": ("0.5", "0"),
}


@kopf.on.create(GROUP, VERSION, KIND)
def on_create(name, namespace, spec, patch, body, **kwargs):
    logger.info(f"[ycsb] Suite {name} created in {namespace}")
    initialize_suite(name, namespace, spec, patch, suite_kind=KIND)
    secret_name = spec.get("database", {}).get("credentialsSecret")
    db_type = spec.get("database", {}).get("type", "ycsb")
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
            parse_results=parse_ycsb, benchmark_image=BENCHMARK_IMAGE,
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
        plural=PLURAL, parse_results=parse_ycsb,
    )


@kopf.timer(GROUP, VERSION, KIND, interval=10.0,
            when=lambda status, **_: (status or {}).get("phase") in ("Running", "Sleeping"))
def on_timer(name, namespace, status, patch, **kwargs):
    """Wake up after sleepBetweenRuns; back-stop for missed Job events."""
    on_suite_timer(name, namespace, status, patch,
                   plural=PLURAL, parse_results=parse_ycsb)


@kopf.on.delete(GROUP, VERSION, KIND)
def on_delete(name, namespace, spec, status, meta, **kwargs):
    """Database pods go with the suite; PVCs per database.volumes.onSuiteDelete."""
    teardown_db_suite(name, namespace, spec, status, meta["uid"])


def _build_command(params: dict, db_ip: str) -> list[str]:
    """
    run_ycsb mongodb run <db_ip> <workload> <threads> <username> <password>
             <dbname> <recordcount> <opcount> <distribution> <readprop>
             <updateprop> <maxexecutiontime>
    """
    return ["bash", "-c", _run_ycsb("run", params, db_ip, params.get("threads", 4))]


def _build_prepare_command(params: dict, db_ip: str) -> list[str]:
    """
    Load recordCount records: drop usertable, ycsb load, then check the
    count (ycsb exits 0 even when inserts fail). One loader thread per
    client CPU. base skips this when the volumes already hold the records.
    """
    records = params.get("recordCount", 1000000)
    threads = params.get("_clientCpus", 4)
    script = f"""set -euo pipefail
{_run_ycsb("cleanup", params, db_ip, threads)}
{_run_ycsb("prepare", params, db_ip, threads)}
count=$(mongo {_mongo_url(db_ip)} --quiet --eval 'db.usertable.count()' | tail -1)
if [ "$count" != "{records}" ]; then
  echo "LOAD FAILED: expected {records} records, found '$count'"
  exit 1
fi
echo "LOAD VERIFIED: {records} records"
"""
    return ["bash", "-c", script]


def _run_ycsb(action: str, params: dict, db_ip: str, threads: int) -> str:
    """One run_ycsb invocation. The image's workload files are workload<a-f>."""
    workload = params.get("workload", "a")
    read_prop, update_prop = WORKLOAD_PROPS.get(workload, ("0.5", "0.5"))
    # run_ycsb builds mongodb://user:password@host/db itself, so it gets the
    # URL-encoded credentials, from the Job's environment
    head = shlex.join(["./run_ycsb", "mongodb", action, db_ip, f"workload{workload}",
                       str(threads)])
    tail = shlex.join([
        str(params.get("recordCount", 1000000)), str(params.get("operationCount", 100000)),
        params.get("requestDistribution", "uniform"), read_prop, update_prop,
        str(params.get("maxExecutionTime", 120)),
    ])
    return (f'{head} "$SHERLOCK_DB_USER_URL" "$SHERLOCK_DB_PASSWORD_URL" '
            f'"$SHERLOCK_DB_NAME" {tail}')


def _mongo_url(db_ip: str) -> str:
    """Shell word: mongodb:// URL built from the Job's environment."""
    return ('"mongodb://${SHERLOCK_DB_USER_URL}:${SHERLOCK_DB_PASSWORD_URL}'
            f'@{db_ip}:27017/${{SHERLOCK_DB_NAME}}"')
