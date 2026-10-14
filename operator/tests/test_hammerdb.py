"""
tests/test_hammerdb.py — HammerDB (MSSQL TPC-C) command construction and
result parsing for both HammerDB result-line formats.

Run with: python -m pytest operator/tests/test_hammerdb.py -v
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from src.handlers import hammerdb
from src.handlers.base import _get_runtime_secs
from src.parsers.benchmarks import parse_hammerdb

PARAMS = {"virtualUsers": 16, "warehouses": 20, "rampupMins": 1, "testMins": 3,
          "allWarehouses": True,
          "_dbType": "mssql", "_username": "sa", "_password": "x", "_dbName": "tpcc",
          "_saPassword": 'Pa$s"w[0]rd!'}


def _tcl(script: str) -> str:
    """The TCL written by the heredoc in a generated shell script."""
    return script.split("<<'SHERLOCK_TCL'\n", 1)[1].split("\nSHERLOCK_TCL\n", 1)[0]


class TestRunCommand:
    def test_timed_driver_with_sweep_params(self):
        cmd = hammerdb._build_command(PARAMS, "10.0.0.7")
        assert cmd[:2] == ["bash", "-c"]
        tcl = _tcl(cmd[2])
        for line in ("dbset db mssqls", "diset connection mssqls_server \"10.0.0.7\"",
                     "diset connection mssqls_linux_authent sql",
                     "diset tpcc mssqls_driver timed", "diset tpcc mssqls_rampup 1",
                     "diset tpcc mssqls_duration 3", "diset tpcc mssqls_allwarehouse true",
                     "diset tpcc mssqls_dbase \"tpcc\"", "vuset vu 16", "vurun"):
            assert line in tcl
        # timer back-stop = (rampup + test) minutes + 180s
        assert "$x < 420" in tcl
        assert "hammerdbcli auto /tmp/sherlock.tcl" in cmd[2]

    def test_password_is_quoted_for_tcl(self):
        tcl = _tcl(hammerdb._build_command(PARAMS, "10.0.0.7")[2])
        assert r'diset connection mssqls_pass "Pa\$s\"w\[0\]rd!"' in tcl


class TestPrepareCommand:
    def test_drops_builds_and_verifies(self):
        script = hammerdb._build_prepare_command(PARAMS, "10.0.0.7")[2]
        assert script.startswith("set -euo pipefail")
        drop = script.index("DROP DATABASE [tpcc]")
        build = script.index("hammerdbcli auto")
        verify = script.index("SELECT COUNT(*) FROM dbo.warehouse")
        assert drop < build < verify
        assert '"$count" != "20"' in script
        tcl = _tcl(script)
        assert "diset tpcc mssqls_count_ware 20" in tcl
        assert f"diset tpcc mssqls_num_vu {hammerdb.BUILD_VIRTUAL_USERS}" in tcl
        assert "buildschema" in tcl

    def test_build_vus_not_above_warehouses(self):
        tcl = _tcl(hammerdb._build_prepare_command({**PARAMS, "warehouses": 2}, "ip")[2])
        assert "diset tpcc mssqls_num_vu 2" in tcl

    def test_sa_password_shell_quoted(self):
        script = hammerdb._build_prepare_command(PARAMS, "10.0.0.7")[2]
        assert "-P 'Pa$s\"w[0]rd!'" in script

    def test_rejects_unsafe_database_name(self):
        with pytest.raises(ValueError):
            hammerdb._build_prepare_command({**PARAMS, "_dbName": "tpcc]; DROP"}, "ip")


class TestParser:
    def test_hammerdb_3_format(self):
        log = ("Vuser 1:3 Active Virtual Users configured\n"
               "Vuser 1:TEST RESULT : System achieved 125678 SQL Server TPM at 45231 NOPM\n")
        assert parse_hammerdb(log, {}) == {"nopm": 45231, "tpm": 125678}

    def test_hammerdb_4_format(self):
        log = "Vuser 1:TEST RESULT : System achieved 45,231 NOPM from 125,678 SQL Server TPM\n"
        assert parse_hammerdb(log, {}) == {"nopm": 45231, "tpm": 125678}

    def test_no_result_line(self):
        assert parse_hammerdb("Vuser 1:Error: connection failed\n", {}) == {}


def test_stats_cover_rampup_and_measurement():
    assert _get_runtime_secs({"testMins": 3, "rampupMins": 1}, {}) == 240
