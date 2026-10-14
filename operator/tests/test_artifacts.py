"""
tests/test_artifacts.py — pvc artifact destination and retention.

The Kubernetes API is replaced by FakeK8s, which records ConfigMaps, Jobs
and PVCs and lets each test choose how the writer Job ends.

Run with: python -m pytest operator/tests/test_artifacts.py -v
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import hashlib

import pytest

from src.utils import artifacts, k8s

NS = "sherlock"
OWNER = {"apiVersion": "sherlock.io/v1alpha1", "kind": "SherlockPgbenchSuite",
         "metadata": {"name": "smoke", "uid": "abcdef12-3456-7890"}}
PVC_SPEC = {"artifacts": {"destination": {"type": "pvc", "pvc": {"size": "5Gi"}}}}


class FakeK8s:
    def __init__(self):
        self.configmaps = {}
        self.deleted_configmaps = []
        self.jobs = {}
        self.pvcs = {}
        self.job_result = "Complete"

    def create_pvc(self, name, namespace, storage_size, storage_class="",
                   access_mode="ReadWriteOnce", labels=None, **kw):
        self.pvcs[name] = {"size": storage_size, "accessMode": access_mode}

    def create_configmap(self, name, namespace, data=None, labels=None,
                         binary_data=None, owner=None):
        self.configmaps[name] = binary_data

    def delete_configmap(self, name, namespace):
        self.deleted_configmaps.append(name)

    def create_job(self, **kw):
        self.jobs[kw["name"]] = kw

    def get_job_state(self, name, namespace):
        return self.job_result

    def get_job_pod_logs(self, name, namespace):
        return "sha256sum: WARNING: 1 computed checksum did NOT match"


@pytest.fixture
def fake(monkeypatch):
    f = FakeK8s()
    for fn in ("create_pvc", "create_configmap", "delete_configmap",
               "create_job", "get_job_state", "get_job_pod_logs"):
        monkeypatch.setattr(k8s, fn, getattr(f, fn))
    monkeypatch.setattr("time.sleep", lambda s: None)
    return f


def _store(bundle=b"bundle-bytes", spec=PVC_SPEC):
    return artifacts.store_bundle(
        bundle=bundle, run_name="smoke-r0", suite_name="smoke", namespace=NS,
        artifacts_spec=spec["artifacts"], owner=OWNER,
    )


class TestPvcDestination:
    def test_writes_through_job_and_records_path(self, fake):
        record = _store()
        assert record["path"] == "pvc://smoke-artifacts/smoke-abcdef12/smoke-r0.tar.gz"
        assert record["sizeBytes"] == len(b"bundle-bytes")
        assert fake.pvcs == {"smoke-artifacts": {"size": "5Gi", "accessMode": "ReadWriteOnce"}}

        (job,) = fake.jobs.values()
        assert job["node_name"] is None
        assert job["owner"] is OWNER
        assert job["pvc_mounts"] == [{"pvc_name": "smoke-artifacts", "mount_path": "/artifacts"}]
        env = job["env_vars"]
        assert env["BUNDLE_DIR"] == "smoke-abcdef12"
        assert env["BUNDLE_FILE"] == "smoke-r0.tar.gz"
        assert env["BUNDLE_SHA256"] == hashlib.sha256(b"bundle-bytes").hexdigest()

    def test_large_bundle_is_split_in_order_and_configmaps_removed(self, fake):
        bundle = os.urandom(artifacts.CHUNK_BYTES * 2 + 10)
        _store(bundle)
        (job,) = fake.jobs.values()
        items = job["configmap_mount"]["items"]
        assert [path for _, _, path in items] == ["chunk-0000", "chunk-0001", "chunk-0002"]
        joined = b"".join(fake.configmaps[cm]["chunk"] for cm, _, _ in items)
        assert joined == bundle
        assert all(len(fake.configmaps[cm]["chunk"]) <= artifacts.CHUNK_BYTES
                   for cm, _, _ in items)
        assert sorted(fake.deleted_configmaps) == sorted(cm for cm, _, _ in items)

    def test_failed_writer_raises_and_still_removes_configmaps(self, fake):
        fake.job_result = "Failed"
        with pytest.raises(RuntimeError, match="checksum"):
            _store()
        assert fake.deleted_configmaps == list(fake.configmaps)

    def test_access_mode_passed_through(self, fake):
        spec = {"artifacts": {"destination": {"type": "pvc",
                                              "pvc": {"accessMode": "ReadWriteMany"}}}}
        _store(spec=spec)
        assert fake.pvcs["smoke-artifacts"]["accessMode"] == "ReadWriteMany"


def _bundle(i, path=None):
    return {"runName": f"r{i}", "path": path or f"pvc://smoke-artifacts/smoke-abcdef12/r{i}.tar.gz",
            "sizeBytes": 1, "collectedAt": f"2026-10-14T00:00:{i:02d}+00:00"}


class TestRetention:
    def test_keep_last_n_deletes_oldest_pvc_bundles_in_one_job(self, fake):
        bundles = [_bundle(i) for i in range(5)]
        surviving = artifacts.enforce_retention(
            "smoke", {"retention": {"keepLastN": 2}}, bundles, namespace=NS, owner=OWNER)
        assert [b["runName"] for b in surviving] == ["r3", "r4"]
        (job,) = fake.jobs.values()
        assert job["command"][:4] == ["sh", "-c", 'rm -fv "$@"', "rm"]
        assert job["command"][4:] == [f"/artifacts/smoke-abcdef12/r{i}.tar.gz" for i in range(3)]

    def test_no_policy_keeps_everything(self, fake):
        bundles = [_bundle(i) for i in range(3)]
        assert artifacts.enforce_retention("smoke", {}, bundles) == bundles
        assert fake.jobs == {}

    def test_path_traversal_not_deleted(self, fake):
        bundles = [_bundle(0, "pvc://smoke-artifacts/../etc/x"), _bundle(1)]
        artifacts.enforce_retention("smoke", {"retention": {"keepLastN": 1}}, bundles,
                                    namespace=NS, owner=OWNER)
        assert fake.jobs == {}
