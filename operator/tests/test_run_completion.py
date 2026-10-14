"""
tests/test_run_completion.py — Run completion, claiming, failure policy, timer.

The Kubernetes API is replaced by FakeCluster, which keeps one suite object,
Job states and logs in memory and enforces the resourceVersion precondition
the same way the API server does (409 on mismatch).

Run with: python -m pytest operator/tests/test_run_completion.py -v
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import copy
from datetime import datetime, timedelta, timezone

import pytest
from kubernetes.client.rest import ApiException

from src.handlers import base
from src.parsers.benchmarks import parse_pgbench

PLURAL = "sherlockpgbenchsuites"
UID = "uid-current"
JOBS = ["postgresql-0-run-aaaa", "postgresql-1-run-bbbb", "postgresql-2-run-cccc"]
TPS = {JOBS[0]: 15470.12, JOBS[1]: 15461.40, JOBS[2]: 16482.28}


def _log(tps):
    return (f"latency average = 0.129 ms\nlatency stddev = 0.05 ms\n"
            f"tps = {tps} (excluding connections establishing)\n")


def _suite(runs=2, failure_policy="abortAll", sleep=10):
    matrix = [
        {"name": f"smoke-r{i}", "params": {"duration": 30}, "phase": "Pending",
         "retryCount": 0}
        for i in range(runs)
    ]
    matrix[0].update({"phase": "Running", "startTime": "2026-10-13T10:00:00+00:00",
                      "benchmarkJobs": list(JOBS), "statJobs": []})
    return {
        "apiVersion": "sherlock.io/v1alpha1",
        "kind": "SherlockPgbenchSuite",
        "metadata": {"name": "smoke", "uid": UID, "resourceVersion": "1"},
        "spec": {
            "execution": {"failurePolicy": failure_policy, "retryLimit": 2,
                          "sleepBetweenRuns": sleep},
            "artifacts": {"collectStats": False},
        },
        "status": {
            "phase": "Running",
            "runMatrix": matrix,
            "summary": {"totalRuns": runs, "runsCompleted": 0, "runsFailed": 0,
                        "runsRemaining": runs, "currentRunName": "smoke-r0"},
        },
    }


class FakeCluster:
    def __init__(self, suite):
        self.suite = suite
        self.job_states = {j: "Active" for j in JOBS}
        self.logs = {j: _log(TPS[j]) for j in JOBS}
        self.patches = []
        self.conflict_hook = None  # called before a conditional patch is applied

    def get_custom_resource(self, group, version, plural, name, namespace):
        return copy.deepcopy(self.suite)

    def get_job_state(self, name, namespace):
        return self.job_states.get(name, "Missing")

    def get_job_pod_logs(self, name, namespace):
        return self.logs.get(name, "")

    def patch_suite_status(self, group, version, plural, name, namespace,
                           status_patch, resource_version=None):
        if resource_version is not None and self.conflict_hook:
            hook, self.conflict_hook = self.conflict_hook, None
            hook(self)
        if resource_version is not None and \
                resource_version != self.suite["metadata"]["resourceVersion"]:
            raise ApiException(status=409, reason="Conflict")
        self.patches.append(copy.deepcopy(status_patch))
        for k, v in status_patch.items():
            if v is None:
                self.suite["status"].pop(k, None)
            else:
                self.suite["status"][k] = copy.deepcopy(v)
        self._bump()

    def _bump(self):
        rv = int(self.suite["metadata"]["resourceVersion"]) + 1
        self.suite["metadata"]["resourceVersion"] = str(rv)


@pytest.fixture
def cluster(monkeypatch):
    def make(**kwargs):
        c = FakeCluster(_suite(**kwargs))
        for fn in ("get_custom_resource", "get_job_state",
                   "get_job_pod_logs", "patch_suite_status"):
            monkeypatch.setattr(base.k8s, fn, getattr(c, fn))
        monkeypatch.setattr(base, "build_bundle", lambda **kw: b"")
        monkeypatch.setattr(base, "store_bundle", lambda **kw: "/artifacts/x.tgz")
        monkeypatch.setattr(base, "enforce_retention", lambda *a, **kw: None)
        return c
    return make


def _event(c, job, state="Complete", event_type="MODIFIED", owner_uid=UID):
    c.job_states[job] = state
    return base.on_benchmark_job_event(
        event_type=event_type, job_name=job, namespace="sherlock",
        job_meta={
            "labels": {"sherlock.io/suite-name": "smoke",
                       "sherlock.io/run-name": "smoke-r0",
                       "sherlock.io/role": "benchmark"},
            "ownerReferences": [{"uid": owner_uid}] if owner_uid else [],
        },
        job_status={"conditions": [{"type": state, "status": "True"}]},
        plural=PLURAL, parse_results=parse_pgbench,
    )


def _run(c, i=0):
    return c.suite["status"]["runMatrix"][i]


class TestCompletion:
    def test_waits_for_all_jobs_then_combines_results(self, cluster):
        c = cluster()
        _event(c, JOBS[0])
        _event(c, JOBS[1])
        assert c.patches == []                       # 2 of 3 done: nothing yet
        _event(c, JOBS[2])

        run = _run(c)
        assert run["phase"] == "Completed"
        assert run["result"]["tps"] == pytest.approx(47413.8, abs=0.01)
        assert [r["job"] for r in run["instanceResults"]] == JOBS
        assert run["startTime"] == "2026-10-13T10:00:00+00:00"
        assert run["artifactPath"] == "/artifacts/x.tgz"
        assert c.suite["status"]["summary"]["runsCompleted"] == 1
        # More runs pending and sleepBetweenRuns > 0 → Sleeping with a wake time
        assert c.suite["status"]["phase"] == "Sleeping"
        assert "nextRunAt" in c.suite["status"]
        # Exactly one claim patch and one result patch
        assert [p.get("phase") for p in c.patches] == ["Collecting", "Sleeping"]

    def test_last_run_finalizes_suite(self, cluster):
        c = cluster(runs=1)
        for j in JOBS:
            _event(c, j)
        assert c.suite["status"]["phase"] == "Completed"

    def test_no_sleep_goes_straight_to_ready(self, cluster):
        c = cluster(sleep=0)
        for j in JOBS:
            _event(c, j)
        assert c.suite["status"]["phase"] == "Ready"

    def test_duplicate_events_complete_once(self, cluster):
        c = cluster()
        for j in JOBS:
            _event(c, j)
        before = len(c.patches)
        _event(c, JOBS[2])                           # replayed / duplicate event
        assert len(c.patches) == before
        assert c.suite["status"]["summary"]["runsCompleted"] == 1

    def test_lost_claim_race_backs_off(self, cluster):
        """Another handler claims the run between our read and our patch."""
        c = cluster()
        for j in JOBS[:2]:
            _event(c, j)

        def other_handler_wins(cl):
            cl.suite["status"]["phase"] = "Collecting"
            cl._bump()
        c.conflict_hook = other_handler_wins

        assert _event(c, JOBS[2]) is None
        assert c.patches == []                       # we wrote nothing
        assert _run(c)["phase"] == "Running"         # left to the winner

    def test_ignores_deleted_events(self, cluster):
        c = cluster()
        for j in JOBS[:2]:
            _event(c, j)
        _event(c, JOBS[2], event_type="DELETED")
        assert c.patches == []

    def test_ignores_stale_job_from_previous_suite(self, cluster):
        c = cluster()
        c.job_states = {j: "Complete" for j in JOBS}
        _event(c, JOBS[0], owner_uid="uid-of-deleted-suite")
        _event(c, JOBS[0], owner_uid=None)           # pre-fix Job, no owner
        assert c.patches == []

    def test_ignores_job_not_in_current_run(self, cluster):
        c = cluster()
        c.job_states = {j: "Complete" for j in JOBS}
        _event(c, "postgresql-0-run-old")
        assert c.patches == []

    def test_timer_completes_run_when_events_were_missed(self, cluster):
        c = cluster()
        c.job_states = {j: "Complete" for j in JOBS}
        patch = base._SuitePatch(PLURAL)
        base.on_suite_timer("smoke", "sherlock", c.suite["status"], patch,
                            plural=PLURAL, parse_results=parse_pgbench)
        assert _run(c)["phase"] == "Completed"


class TestFailures:
    def test_failed_job_aborts_suite(self, cluster):
        c = cluster(failure_policy="abortAll")
        _event(c, JOBS[0])
        _event(c, JOBS[1], state="Failed")
        assert c.patches == []                       # still waiting for JOBS[2]
        _event(c, JOBS[2])
        assert _run(c)["phase"] == "Failed"
        assert c.suite["status"]["phase"] == "Failed"
        assert c.suite["status"]["summary"]["runsFailed"] == 1

    def test_exit_zero_without_metrics_is_a_failure(self, cluster):
        """run_pgbench exits 0 even when pgbench fails (open issue 5)."""
        c = cluster(failure_policy="abortAll")
        c.logs[JOBS[1]] = 'ERROR: relation "pgbench_branches" does not exist\n'
        for j in JOBS:
            _event(c, j)
        assert _run(c)["phase"] == "Failed"
        assert c.suite["status"]["phase"] == "Failed"

    def test_missing_job_is_a_failure(self, cluster):
        c = cluster(failure_policy="abortAll")
        c.job_states = {JOBS[0]: "Complete", JOBS[1]: "Complete"}  # JOBS[2] gone
        _event(c, JOBS[0])
        assert c.suite["status"]["phase"] == "Failed"

    def test_retry_resets_run_and_sleeps(self, cluster):
        c = cluster(failure_policy="retryN")
        _event(c, JOBS[0])
        _event(c, JOBS[1])
        _event(c, JOBS[2], state="Failed")
        run = _run(c)
        assert run["phase"] == "Pending"
        assert run["retryCount"] == 1
        assert run["benchmarkJobs"] == []
        assert c.suite["status"]["phase"] == "Sleeping"

    def test_continue_and_report_moves_on(self, cluster):
        c = cluster(failure_policy="continueAndReport")
        _event(c, JOBS[0])
        _event(c, JOBS[1])
        _event(c, JOBS[2], state="Failed")
        assert _run(c)["phase"] == "Failed"
        assert c.suite["status"]["phase"] == "Sleeping"

    def test_continue_and_report_last_run_is_degraded(self, cluster):
        c = cluster(runs=1, failure_policy="continueAndReport")
        _event(c, JOBS[0])
        _event(c, JOBS[1])
        _event(c, JOBS[2], state="Failed")
        assert c.suite["status"]["phase"] == "Degraded"


class TestTimer:
    def _tick(self, status):
        patch = base._SuitePatch(PLURAL)
        base.on_suite_timer("smoke", "sherlock", status, patch,
                            plural=PLURAL, parse_results=parse_pgbench)
        return patch.status

    def test_wakes_when_due(self):
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        assert self._tick({"phase": "Sleeping", "nextRunAt": past}) == \
            {"phase": "Ready", "nextRunAt": None}

    def test_keeps_sleeping_until_due(self):
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        assert self._tick({"phase": "Sleeping", "nextRunAt": future}) == {}

    def test_ignores_other_phases(self):
        assert self._tick({"phase": "Completed"}) == {}


def test_update_run_phase_keeps_earlier_writes():
    """Issue 7: the second call used to drop the startTime set by the first."""
    matrix = [{"name": "r0", "phase": "Pending"}, {"name": "r1", "phase": "Pending"}]
    patch = base._SuitePatch(PLURAL)
    base._update_run_phase(patch, matrix, "r0", "Running", start_time="T0")
    base._update_run_phase(patch, matrix, "r0", "Running",
                           extra={"benchmarkJobs": ["j"]})
    run = patch.status["runMatrix"][0]
    assert run["startTime"] == "T0"
    assert run["benchmarkJobs"] == ["j"]
