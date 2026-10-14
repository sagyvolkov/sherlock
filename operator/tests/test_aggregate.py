"""
tests/test_aggregate.py — Combining per-instance results, and Job naming.

Run with: python -m pytest operator/tests/test_aggregate.py -v
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import math

import pytest
from src.parsers.aggregate import aggregate_results
from src.parsers.benchmarks import parse_pgbench
from src.utils.k8s import job_name, parse_duration_secs, job_state_from_conditions


def _pgbench_log(tps: float, lat: float, stddev: float = 0.05) -> str:
    return (
        "transaction type: <builtin: select only>\n"
        f"latency average = {lat} ms\n"
        f"latency stddev = {stddev} ms\n"
        f"tps = {tps} (without initial connection time)\n"
        f"tps = {tps} (excluding connections establishing)\n"
    )


class TestAggregate:
    def test_smoke_test_numbers_are_summed(self):
        """The 2026-10-13 smoke test: 3 instances should add up to ~47,400 TPS."""
        per_instance = [
            parse_pgbench(_pgbench_log(15470.12, 0.129), {}),
            parse_pgbench(_pgbench_log(15461.40, 0.129), {}),
            parse_pgbench(_pgbench_log(16482.28, 0.121), {}),
        ]
        result = aggregate_results(per_instance)
        assert result["tps"] == pytest.approx(47413.8, abs=0.01)
        # Weighted toward the faster instance's lower latency
        assert 0.121 < result["latencyAvgMs"] < 0.129
        assert result["latencyAvgMs"] == pytest.approx(
            (15470.12 * 0.129 + 15461.40 * 0.129 + 16482.28 * 0.121) / 47413.8,
            abs=0.001,
        )

    def test_single_instance_is_unchanged(self):
        r = {"tps": 100.0, "latencyAvgMs": 2.0, "latencyStddevMs": 0.5}
        assert aggregate_results([r]) == r

    def test_empty_results_are_ignored(self):
        assert aggregate_results([]) == {}
        assert aggregate_results([{}, {}]) == {}
        assert aggregate_results([{}, {"tps": 5.0}]) == {"tps": 5.0}

    def test_pooled_stddev(self):
        # Two equal-weight instances, means 1 and 3, stddev 1 each:
        # pooled variance = 1 + 1 (spread of the means) = 2
        r = aggregate_results([
            {"tps": 10.0, "latencyAvgMs": 1.0, "latencyStddevMs": 1.0},
            {"tps": 10.0, "latencyAvgMs": 3.0, "latencyStddevMs": 1.0},
        ])
        assert r["latencyAvgMs"] == pytest.approx(2.0)
        assert r["latencyStddevMs"] == pytest.approx(math.sqrt(2), abs=0.001)

    def test_percentiles_and_max_take_the_worst_instance(self):
        r = aggregate_results([
            {"tps": 10.0, "qps": 200.0, "latencyP95Ms": 3.0, "latencyMaxMs": 40.0},
            {"tps": 30.0, "qps": 600.0, "latencyP95Ms": 5.0, "latencyMaxMs": 20.0},
        ])
        assert r["tps"] == 40.0
        assert r["qps"] == 800.0
        assert r["latencyP95Ms"] == 5.0
        assert r["latencyMaxMs"] == 40.0

    def test_hammerdb_and_ycsb_and_fio_keys(self):
        r = aggregate_results([
            {"nopm": 1000, "tpm": 2300},
            {"nopm": 1500, "tpm": 3400},
        ])
        assert r == {"nopm": 2500, "tpm": 5700}

        r = aggregate_results([
            {"throughputOpsPerSec": 100.0, "readLatencyAvgUs": 200.0, "readLatencyP99Us": 900.0},
            {"throughputOpsPerSec": 300.0, "readLatencyAvgUs": 100.0, "readLatencyP99Us": 700.0},
        ])
        assert r["throughputOpsPerSec"] == 400.0
        assert r["readLatencyAvgUs"] == pytest.approx(125.0)
        assert r["readLatencyP99Us"] == 900.0

        r = aggregate_results([
            {"readIops": 1000.0, "totalIops": 1000.0, "readBwMBs": 4.0, "readLatAvgUs": 100.0},
            {"readIops": 3000.0, "totalIops": 3000.0, "readBwMBs": 12.0, "readLatAvgUs": 50.0},
        ])
        assert r["readIops"] == 4000.0
        assert r["readBwMBs"] == 16.0
        assert r["readLatAvgUs"] == pytest.approx(62.5)


class TestJobNames:
    def test_fits_and_is_unique(self):
        long_run = "pgbench-smoke-test-c2-t2-rw100-sf10-d30-psimple"
        a = job_name("postgresql-0-run", f"uid-1/{long_run}/0/bench/postgresql-0")
        b = job_name("postgresql-0-run", f"uid-1/{long_run}-x/0/bench/postgresql-0")
        c = job_name("postgresql-0-run", f"uid-2/{long_run}/0/bench/postgresql-0")
        d = job_name("postgresql-0-run", f"uid-1/{long_run}/1/bench/postgresql-0")
        assert len({a, b, c, d}) == 4
        assert a.startswith("postgresql-0-run-")

    def test_long_prefix_is_truncated_to_63(self):
        name = job_name("a-very-long-suite-name-" * 5, "key")
        assert len(name) <= 63
        assert not name.split("-")[-2].endswith(".")

    def test_deterministic(self):
        assert job_name("p", "k") == job_name("p", "k")


def test_parse_duration_secs():
    assert parse_duration_secs("30m", 1) == 1800
    assert parse_duration_secs("1h", 1) == 3600
    assert parse_duration_secs(None, 42) == 42
    assert parse_duration_secs("bogus", 42) == 42


def test_job_state_from_conditions():
    assert job_state_from_conditions([]) == "Active"
    assert job_state_from_conditions(
        [{"type": "SuccessCriteriaMet", "status": "True"}]) == "Active"
    assert job_state_from_conditions(
        [{"type": "Complete", "status": "True"}]) == "Complete"
    assert job_state_from_conditions(
        [{"type": "Failed", "status": "True"}]) == "Failed"
    assert job_state_from_conditions(
        [{"type": "Failed", "status": "False"}]) == "Active"
