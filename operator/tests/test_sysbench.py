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
import kopf
from kubernetes import client
from kubernetes.client.rest import ApiException

from src.handlers import base, sysbench
from src.utils import credentials

PARAMS = {"threads": 16, "tables": 4, "tableSize": 10000, "testType": "oltp_read_only",
          "duration": 60, "reportInterval": 5,
          "_dbType": "mysql", "_username": "bench", "_dbName": "sb"}


def _words(cmd):
    """Shell words of a bash -c command (env refs stay as $VARS)."""
    assert cmd[:2] == ["bash", "-c"]
    return shlex.split(cmd[2])


class TestCommands:
    def test_run_uses_test_type_and_all_options(self):
        cmd = _words(sysbench._build_command(PARAMS, "10.0.0.5"))
        assert cmd[:2] == ["sysbench", "oltp_read_only"]
        assert cmd[-1] == "run"
        for opt in ("--db-driver=mysql", "--mysql-host=10.0.0.5", "--mysql-port=3306",
                    "--mysql-user=$SHERLOCK_DB_USER", "--mysql-password=$SHERLOCK_DB_PASSWORD",
                    "--mysql-db=$SHERLOCK_DB_NAME",
                    "--tables=4", "--table-size=10000", "--threads=16", "--time=60",
                    "--report-interval=5"):
            assert opt in cmd
        assert not any("ignore-errors" in c for c in cmd)

    def test_postgresql_uses_pgsql_driver(self):
        cmd = _words(sysbench._build_command({**PARAMS, "_dbType": "postgresql"}, "10.0.0.5"))
        assert "--db-driver=pgsql" in cmd and "--pgsql-port=5432" in cmd
        assert "--pgsql-password=$SHERLOCK_DB_PASSWORD" in cmd

    def test_prepare_cleans_then_loads_with_capped_threads(self):
        shell = sysbench._build_prepare_command({**PARAMS, "tables": 32}, "10.0.0.5")
        assert shell[:2] == ["bash", "-c"]
        cleanup, prepare = shell[2].split(" && ")
        cleanup, prepare = shlex.split(cleanup), shlex.split(prepare)
        assert cleanup[1] == prepare[1] == "oltp_read_write"
        assert cleanup[-1] == "cleanup" and prepare[-1] == "prepare"
        assert f"--threads={sysbench.PREPARE_THREADS}" in prepare
        assert "--tables=32" in prepare

    def test_prepare_threads_not_above_table_count(self):
        shell = sysbench._build_prepare_command(PARAMS, "10.0.0.5")
        assert "--threads=4" in shell[2]

    def test_password_env_ref_is_double_quoted(self):
        # double quotes: the shell expands the variable, and a password with
        # spaces stays one argument
        assert '--mysql-password="$SHERLOCK_DB_PASSWORD"' in sysbench._build_command(PARAMS, "ip")[2]


class TestCommandParams:
    def test_adds_connection_details_but_no_password(self):
        params = {"threads": 4}
        spec = {"database": {"type": "mysql", "credentialsSecret": "my-creds"}}
        status = {"dbCredentials": {"username": "u", "dbName": "d"}}
        out = base._command_params(spec, status, params)
        assert out == {"threads": 4, "_dbType": "mysql", "_username": "u",
                       "_dbName": "d", "_clientCpus": 4}
        assert params == {"threads": 4}


class TestClientResources:
    DB = {"limits": {"cpu": "2", "memory": "4Gi"}}

    def test_client_resources_preferred_over_database(self):
        spec = {"database": {"resources": self.DB},
                "execution": {"clientResources": {"limits": {"cpu": "16"}}}}
        assert base.client_resources(spec) == {"limits": {"cpu": "16"}}
        assert base.client_cpus(spec) == 16

    def test_falls_back_to_database_resources(self):
        spec = {"database": {"resources": self.DB}, "execution": {}}
        assert base.client_resources(spec) == self.DB
        assert base.client_cpus(spec) == 2

    @pytest.mark.parametrize("res, cpus", [
        ({}, 4),                                    # Job default
        ({"requests": {"cpu": "3"}}, 3),            # requests when no limit
        ({"limits": {"cpu": "500m"}}, 1),           # never below 1
        ({"limits": {"cpu": "2500m"}}, 2),
        ({"limits": {"cpu": "7.5"}}, 7),
    ])
    def test_cpu_parsing(self, res, cpus):
        assert base.client_cpus({"execution": {"clientResources": res}}) == cpus


class FakeCore:
    def __init__(self, data=None):
        self.data, self.created = data, None

    def read_namespaced_secret(self, name, namespace):
        if self.data is None:
            raise ApiException(status=404)
        enc = lambda v: base64.b64encode(v.encode()).decode()
        return client.V1Secret(data={k: enc(v) for k, v in self.data.items()})


class TestCredentials:
    def test_secret_values_are_base64_decoded(self, monkeypatch):
        core = FakeCore({"username": "bench", "password": "s3cret", "dbName": "sb"})
        monkeypatch.setattr(credentials.client, "CoreV1Api", lambda: core)
        assert credentials.resolve("mysql", "sherlock", "my-creds") == {
            "secret": "my-creds", "username": "bench", "password": "s3cret", "dbName": "sb"}

    def test_mssql_uses_sa_and_tpcc(self, monkeypatch):
        core = FakeCore({"username": "bench", "password": "S3cret!x", "dbName": "sb"})
        monkeypatch.setattr(credentials.client, "CoreV1Api", lambda: core)
        assert credentials.resolve("mssql", "sherlock", None) == {
            "secret": "sherlock-db-credentials", "username": "sa",
            "password": "S3cret!x", "dbName": "tpcc"}

    def test_default_secret_created_when_missing(self, monkeypatch):
        created = {}
        monkeypatch.setattr(credentials.client, "CoreV1Api", lambda: FakeCore())
        monkeypatch.setattr("src.utils.k8s.create_secret",
                            lambda name, ns, data, owner=None, labels=None: created.update(
                                name=name, data=data))
        creds = credentials.resolve("postgresql", "sherlock", None)
        assert created == {"name": "sherlock-db-credentials", "data": credentials.DEFAULTS}
        assert creds["password"] == credentials.DEFAULTS["password"]

    def test_named_secret_must_exist(self, monkeypatch):
        monkeypatch.setattr(credentials.client, "CoreV1Api", lambda: FakeCore())
        with pytest.raises(kopf.PermanentError):
            credentials.resolve("mysql", "sherlock", "nope")

    def test_client_env(self):
        env = credentials.client_secret_env("sherlock-db-credentials", "mssql")
        assert env["SHERLOCK_DB_PASSWORD"] == ("sherlock-db-credentials", "password")
        assert env["SQLCMDPASSWORD"] == ("sherlock-db-credentials", "password")
        assert "SQLCMDPASSWORD" not in credentials.client_secret_env("s", "mysql")

    def test_database_pods_get_passwords_from_secret(self):
        from src.db.deploy import _build_env_vars
        for db in ("postgresql", "mysql", "mssql", "mongodb"):
            env = _build_env_vars(db, "u", "d", "sherlock-db-credentials")
            secret_refs = {k for k, v in env.items() if isinstance(v, dict)}
            plain = {k: v for k, v in env.items() if not isinstance(v, dict)}
            assert secret_refs, db
            assert not any("PASSWORD" in k for k in plain), (db, plain)


def test_pgbench_command_takes_credentials_from_environment():
    from src.handlers import pgbench
    cmd = pgbench._build_command({"clients": 2, "threads": 2, "duration": 30,
                                  "readWriteRatio": 100, "scaleFactor": 10}, "10.0.0.1")
    words = shlex.split(cmd[2])
    assert words[10:13] == ["$SHERLOCK_DB_USER", "$SHERLOCK_DB_PASSWORD", "$SHERLOCK_DB_NAME"]
    assert '"$SHERLOCK_DB_PASSWORD"' in cmd[2]
