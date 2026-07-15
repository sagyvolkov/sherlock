"""
tests/test_matrix.py — Unit tests for the parameter sweep matrix expander.

Tests the cartesian product expansion and run name generation that replaces
the nested for-loops in the original sherlock bash scripts.

Run with: python -m pytest operator/tests/test_matrix.py -v
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from src.utils.matrix import expand, make_run_name, count_runs


# ── expand() ─────────────────────────────────────────────────────────────────

class TestExpand:

    def test_single_value_fields_give_one_result(self):
        """Single-value lists produce exactly one combination."""
        sweep = {
            "virtualUsers": [8],
            "warehouses":   [100],
            "testMins":     [5],
        }
        result = expand(sweep)
        assert len(result) == 1
        assert result[0] == {"virtualUsers": 8, "warehouses": 100, "testMins": 5}

    def test_two_axes_cartesian_product(self):
        """Two multi-value fields produce N×M combinations."""
        sweep = {
            "virtualUsers": [8, 16, 32],
            "testMins":     [5, 10],
        }
        result = expand(sweep)
        assert len(result) == 6  # 3 × 2

        vus  = [r["virtualUsers"] for r in result]
        mins = [r["testMins"]     for r in result]

        # All VU values appear twice (once per testMins value)
        assert vus.count(8)  == 2
        assert vus.count(16) == 2
        assert vus.count(32) == 2

        # All testMins values appear three times (once per VU value)
        assert mins.count(5)  == 3
        assert mins.count(10) == 3

    def test_three_axes(self):
        """Three axes expand correctly: 4 × 3 × 1 = 12."""
        sweep = {
            "clients":        [1, 2, 8, 16],
            "readWriteRatio": [100, 70, 50],
            "duration":       [120],
        }
        result = expand(sweep)
        assert len(result) == 12

    def test_fio_style_sweep(self):
        """Matches the original run_fio_tests script sweep pattern."""
        sweep = {
            "blockSize":  ["4k", "8k", "64k", "128k", "1m"],
            "rwMixWrite": [0],
        }
        result = expand(sweep)
        assert len(result) == 5
        block_sizes = [r["blockSize"] for r in result]
        assert block_sizes == ["4k", "8k", "64k", "128k", "1m"]

    def test_slob_style_sweep(self):
        """
        Matches the SLOB script pattern that inspired this design:
          for update_pct in 0 30 50 100:
            for run_time in 120:
              for s in 1 2 16:
                for t in 44:
        Produces 4 × 1 × 3 × 1 = 12 combinations.
        """
        sweep = {
            "updatePct": [0, 30, 50, 100],
            "runTime":   [120],
            "sessions":  [1, 2, 16],
            "threads":   [44],
        }
        result = expand(sweep)
        assert len(result) == 12

    def test_empty_sweep_returns_one_empty_dict(self):
        """Empty sweep produces one run with no params."""
        result = expand({})
        assert result == [{}]

    def test_scalar_values_treated_as_single_element(self):
        """Non-list values are treated as fixed params."""
        sweep = {
            "virtualUsers": [8, 16],
            "warehouses":   100,        # scalar, not a list
        }
        result = expand(sweep)
        assert len(result) == 2
        assert all(r["warehouses"] == 100 for r in result)

    def test_all_combinations_are_unique(self):
        """No duplicate combinations are generated."""
        sweep = {
            "blockSize":   ["4k", "8k", "64k"],
            "rwMixWrite":  [0, 30, 100],
            "ioPattern":   ["randrw", "read"],
        }
        result = expand(sweep)
        assert len(result) == 18
        # Convert to frozensets of tuples for uniqueness check
        as_tuples = [frozenset(r.items()) for r in result]
        assert len(set(as_tuples)) == 18


# ── make_run_name() ───────────────────────────────────────────────────────────

class TestMakeRunName:

    def test_hammerdb_run_name(self):
        """HammerDB run names encode VU and testMins."""
        name = make_run_name(
            "mssql-vu-sweep",
            {"virtualUsers": 16, "warehouses": 100, "rampupMins": 2, "testMins": 5}
        )
        assert "vu16" in name
        assert "wh100" in name
        assert "rm2" in name
        assert "tm5" in name
        assert name.startswith("mssql-vu-sweep")

    def test_fio_run_name(self):
        """fio run names encode block size and rw mix."""
        name = make_run_name(
            "nvme-sweep",
            {"blockSize": "4k", "rwMixWrite": 0, "ioPattern": "randrw"}
        )
        assert "bs4k" in name
        assert "rw0" in name
        assert "randrw" in name

    def test_run_name_is_lowercase(self):
        """k8s names must be lowercase."""
        name = make_run_name("MySweet-Suite", {"virtualUsers": 8})
        assert name == name.lower()

    def test_run_name_max_length(self):
        """Run names must not exceed max_len (default 60 chars)."""
        long_params = {f"key{i}": f"value{i}" for i in range(20)}
        name = make_run_name("suite", long_params, max_len=60)
        assert len(name) <= 60

    def test_run_name_starts_with_suite_name(self):
        """Suite name is always the prefix."""
        name = make_run_name("postgres-rw-sweep", {"clients": 8, "readWriteRatio": 70})
        assert name.startswith("postgres-rw-sweep")

    def test_unknown_param_uses_full_key(self):
        """Params not in the abbrev map use the full key name."""
        name = make_run_name("suite", {"myCustomParam": "foo"})
        assert "mycustomparam" in name.lower()


# ── count_runs() ─────────────────────────────────────────────────────────────

class TestCountRuns:

    def test_count_matches_expand_length(self):
        """count_runs() should equal len(expand())."""
        for sweep in [
            {"virtualUsers": [8, 16, 32], "testMins": [5, 10]},
            {"blockSize": ["4k", "8k", "64k", "128k", "1m"], "rwMixWrite": [0]},
            {"clients": [1, 2, 8, 16], "readWriteRatio": [100, 70, 50], "duration": [120]},
        ]:
            assert count_runs(sweep) == len(expand(sweep))

    def test_count_empty(self):
        assert count_runs({}) == 1
