"""
tests/test_ycsb.py — YCSB (MongoDB) command construction and parsing.

Run with: python -m pytest operator/tests/test_ycsb.py -v
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import shlex

from src.handlers import ycsb
from src.parsers.benchmarks import parse_ycsb

PARAMS = {"threads": 16, "workload": "e", "recordCount": 100000, "operationCount": 5000000,
          "requestDistribution": "zipfian", "maxExecutionTime": 60,
          "_dbType": "mongodb", "_username": "bench", "_password": "p@ss:w/rd",
          "_dbName": "sherlock", "_clientCpus": 6}


def _args(line: str) -> list[str]:
    return shlex.split(line)


def test_run_uses_workload_file_name_and_encoded_credentials():
    cmd = ycsb._build_command(PARAMS, "10.0.0.9")
    assert cmd[:2] == ["bash", "-c"]
    args = _args(cmd[2])
    assert args == ["./run_ycsb", "mongodb", "run", "10.0.0.9", "workloade", "16",
                    "bench", "p%40ss%3Aw%2Frd", "sherlock", "100000", "5000000",
                    "zipfian", "0", "0", "60"]


def test_workload_e_keeps_its_scan_mix():
    # workloade has readproportion=0 (95% scans); the old table set 0.95
    assert ycsb.WORKLOAD_PROPS["e"] == ("0", "0")


def test_prepare_drops_loads_and_verifies_count():
    script = ycsb._build_prepare_command(PARAMS, "10.0.0.9")[2]
    lines = script.splitlines()
    cleanup = next(l for l in lines if " cleanup " in l)
    prepare = next(l for l in lines if " prepare " in l)
    assert lines.index(cleanup) < lines.index(prepare)
    assert _args(prepare)[5] == "6"                     # loader threads = client CPUs
    assert "db.usertable.count()" in script
    assert "mongodb://bench:p%40ss%3Aw%2Frd@10.0.0.9:27017/sherlock" in script
    assert '"$count" != "100000"' in script


def test_parser_counts_operations_that_did_not_return_ok():
    log = ("[OVERALL], Throughput(ops/sec), 1234.5\n"
           "[READ], AverageLatency(us), 300.0\n"
           "[READ], Return=OK, 900\n"
           "[READ], Return=NOT_FOUND, 7\n"
           "[UPDATE-FAILED], Return=ERROR, 3\n"
           "[UPDATE], Return=OK, 90\n")
    r = parse_ycsb(log, {})
    assert r["throughputOpsPerSec"] == 1234.5
    assert r["failedOps"] == 10
