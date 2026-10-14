"""
tests/test_sysbench.py — sysbench command construction, credentials in
command params, and credentials Secret decoding.

Run with: python -m pytest operator/tests/test_sysbench.py -v
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import base64
import shlex

import pytest
from kubernetes import client

from src.handlers import base, sysbench
from src.utils import credentials

PARAMS = {"threads": 16, "tables": 4, "tableSize": 10000, "testType": "oltp_read_only",
          "duration": 60, "reportInterval": 5,
          "_dbType": "mysql", "_username": "bench", "_password": "p@ss word", "_dbName": "sb"}


class TestCommands:
    def test_run_uses_test_type_and_all_options(self):
        cmd = sysbench._build_command(PARAMS, "10.0.0.5")
        assert cmd[:2] == ["sysbench", "oltp_read_only"]
        assert cmd[-1] == "run"
        for opt in ("--db-driver=mysql", "--mysql-host=10.0.0.5", "--mysql-port=3306",
                    "--mysql-user=bench", "--mysql-password=p@ss word", "--mysql-db=sb",
                    "--tables=4", "--table-size=10000", "--threads=16", "--time=60",
                    "--report-interval=5"):
            assert opt in cmd
        assert not any("ignore-errors" in c for c in cmd)

    def test_postgresql_uses_pgsql_driver(self):
        cmd = sysbench._build_command({**PARAMS, "_dbType": "postgresql"}, "10.0.0.5")
        assert "--db-driver=pgsql" in cmd and "--pgsql-port=5432" in cmd

    def test_prepare_cleans_then_loads_with_capped_threads(self):
        shell = sysbench._build_prepare_command({**PARAMS, "tables": 32}, "10.0.0.5")
        assert shell[:2] == ["bash", "-c"]
        cleanup, prepare = shell[2].split(" && ")
        cleanup, prepare = shlex.split(cleanup), shlex.split(prepare)
        assert cleanup[1] == prepare[1] == "oltp_read_write"
        assert cleanup[-1] == "cleanup" and prepare[-1] == "prepare"
        assert f"--threads={sysbench.PREPARE_THREADS}" in prepare
        assert "--mysql-password=p@ss word" in prepare   # quoted correctly
        assert "--tables=32" in prepare

    def test_prepare_threads_not_above_table_count(self):
        shell = sysbench._build_prepare_command(PARAMS, "10.0.0.5")
        assert "--threads=4" in shell[2]


class TestCommandParams:
    def test_adds_credentials_and_type_without_touching_params(self, monkeypatch):
        seen = {}

        def fake_resolve(db_type, namespace, secret):
            seen.update(db_type=db_type, secret=secret)
            return {"username": "u", "password": "p", "dbName": "d"}

        monkeypatch.setattr(base, "resolve_credentials", fake_resolve)
        params = {"threads": 4}
        spec = {"database": {"type": "mysql", "credentialsSecret": "my-creds"}}
        out = base._command_params(spec, "sherlock", params)
        assert out == {"threads": 4, "_dbType": "mysql",
                       "_username": "u", "_password": "p", "_dbName": "d"}
        assert params == {"threads": 4}
        assert seen == {"db_type": "mysql", "secret": "my-creds"}


def test_secret_values_are_base64_decoded(monkeypatch):
    enc = lambda v: base64.b64encode(v.encode()).decode()
    secret = client.V1Secret(data={"username": enc("bench"), "password": enc("s3cret"),
                                   "dbName": enc("sb")})

    class FakeCore:
        def read_namespaced_secret(self, name, namespace):
            return secret

    monkeypatch.setattr(credentials.client, "CoreV1Api", FakeCore)
    assert credentials.resolve("mysql", "sherlock", "my-creds") == {
        "username": "bench", "password": "s3cret", "dbName": "sb"}
