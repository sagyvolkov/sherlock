"""
tests/test_volumes.py — Volume set reuse (utils/volumes.py) and the
marker-based prepare decision in handlers/base.py.

The Kubernetes API is replaced by FakePVCs, which keeps PVCs (as real
kubernetes client models) and suite objects in memory.

Run with: python -m pytest operator/tests/test_volumes.py -v
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json

import pytest
from kubernetes import client

from src.handlers import base
from src.utils import volumes

NS = "sherlock"
KIND = "SherlockPgbenchSuite"


def _owner(name="suite-a", uid="uid-a"):
    return {"apiVersion": "sherlock.io/v1alpha1", "kind": KIND,
            "metadata": {"name": name, "uid": uid}}


def _layout(count=3, scale=10, **kw):
    args = dict(suite_kind=KIND, total=count, instances_per_node=1, workers=count,
                storage_class="lb", storage_size="20Gi", dataset={"scaleFactor": scale},
                database={"type": "postgresql", "image": "postgres:11.7",
                          "username": "sherlock", "password": "pw", "dbName": "sherlock"})
    args.update(kw)
    return volumes.make_layout(**args)


class FakePVCs:
    def __init__(self):
        self.pvcs = {}
        self.suites = {}      # (plural, name) -> object
        self.created = []
        self.deleted = []

    def add(self, name, phase="Bound", annotations=None):
        self.pvcs[name] = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(name=name, namespace=NS,
                                         annotations=dict(annotations or {}), labels={}),
            status=client.V1PersistentVolumeClaimStatus(phase=phase),
        )

    def add_set(self, prefix, count, layout, claimed_by=None):
        for j in range(count):
            ann = {volumes.MARKER_ANNOTATION: json.dumps(layout)} if layout else {}
            if claimed_by:
                ann.update(volumes._claim_annotations(claimed_by))
            self.add(f"{prefix}-pvc-{j}", annotations=ann)

    # ── k8s API surface used by volumes.py ──
    def list_pvcs(self, namespace):
        return list(self.pvcs.values())

    def read_pvc(self, name, namespace):
        return self.pvcs.get(name)

    def create_pvc(self, name, namespace, storage_size, storage_class="",
                   volume_mode="Filesystem", labels=None, annotations=None):
        self.created.append(name)
        self.add(name, annotations=annotations)

    def delete_pvc(self, name, namespace):
        self.deleted.append(name)
        self.pvcs.pop(name, None)

    def patch_pvc_metadata(self, name, namespace, labels=None, annotations=None):
        meta = self.pvcs[name].metadata
        for k, v in (annotations or {}).items():
            if v is None:
                meta.annotations.pop(k, None)
            else:
                meta.annotations[k] = v
        meta.labels.update(labels or {})

    def wait_for_pvcs_bound(self, namespace, names, timeout=600):
        pass

    def wait_for_pvcs_deleted(self, namespace, names, timeout=600):
        assert not set(names) & set(self.pvcs)

    def get_custom_resource(self, group, version, plural, name, namespace):
        return self.suites.get((plural, name))

    def marker(self, name):
        value = self.pvcs[name].metadata.annotations.get(volumes.MARKER_ANNOTATION)
        return json.loads(value) if value else None


@pytest.fixture
def fake(monkeypatch):
    f = FakePVCs()
    for fn in ("list_pvcs", "read_pvc", "create_pvc", "delete_pvc",
               "patch_pvc_metadata", "wait_for_pvcs_bound",
               "wait_for_pvcs_deleted", "get_custom_resource"):
        monkeypatch.setattr(volumes.k8s, fn, getattr(f, fn))
    return f


def _ensure(fake, layout, owner=None, on_create="reuse", prefix="postgresql"):
    return volumes.ensure_volume_set(prefix, NS, layout, owner or _owner(),
                                     on_create=on_create)


class TestEnsureVolumeSet:
    def test_creates_fresh_set_without_marker(self, fake):
        names, reused = _ensure(fake, _layout())
        assert names == ["postgresql-pvc-0", "postgresql-pvc-1", "postgresql-pvc-2"]
        assert not reused
        assert fake.created == names
        assert all(fake.marker(n) is None for n in names)
        ann = fake.pvcs[names[0]].metadata.annotations
        assert ann[volumes.CLAIM_UID] == "uid-a"

    def test_reuses_matching_set_and_claims_it(self, fake):
        fake.add_set("postgresql", 3, _layout(), claimed_by=_owner("old", "uid-old"))
        names, reused = _ensure(fake, _layout())
        assert reused
        assert fake.created == [] and fake.deleted == []
        assert fake.pvcs[names[0]].metadata.annotations[volumes.CLAIM_UID] == "uid-a"

    @pytest.mark.parametrize("change", [
        {"scale": 20},                        # dataset
        {"storage_size": "40Gi"},             # size
        {"storage_class": "other"},           # storage class
        {"instances_per_node": 2},            # layout
        {"database": {"type": "postgresql", "image": "postgres:16",
                      "username": "sherlock", "password": "pw", "dbName": "sherlock"}},
        {"database": {"type": "postgresql", "image": "postgres:11.7",
                      "username": "sherlock", "password": "other", "dbName": "sherlock"}},
    ])
    def test_any_layout_change_recreates_all(self, fake, change):
        fake.add_set("postgresql", 3, _layout())
        names, reused = _ensure(fake, _layout(**change))
        assert not reused
        assert sorted(fake.deleted) == names
        assert fake.created == names

    def test_missing_marker_on_one_pvc_recreates_all(self, fake):
        fake.add_set("postgresql", 3, _layout())
        fake.pvcs["postgresql-pvc-1"].metadata.annotations.clear()
        _, reused = _ensure(fake, _layout())
        assert not reused
        assert len(fake.deleted) == 3

    def test_missing_pvc_recreates_all(self, fake):
        fake.add_set("postgresql", 3, _layout())
        del fake.pvcs["postgresql-pvc-2"]
        _, reused = _ensure(fake, _layout())
        assert not reused
        assert sorted(fake.deleted) == ["postgresql-pvc-0", "postgresql-pvc-1"]

    def test_shrinking_deletes_extras_and_recreates(self, fake):
        fake.add_set("postgresql", 4, _layout(count=4))
        names, reused = _ensure(fake, _layout(count=3))
        assert not reused
        assert len(fake.deleted) == 4      # including postgresql-pvc-3
        assert "postgresql-pvc-3" not in fake.pvcs
        assert fake.created == names

    def test_extra_pvc_with_matching_marker_still_recreates(self, fake):
        fake.add_set("postgresql", 3, _layout())
        fake.add("postgresql-pvc-7", annotations={
            volumes.MARKER_ANNOTATION: json.dumps(_layout())})
        _, reused = _ensure(fake, _layout())
        assert not reused
        assert "postgresql-pvc-7" in fake.deleted

    def test_unbound_pvc_recreates_all(self, fake):
        fake.add_set("postgresql", 3, _layout())
        fake.pvcs["postgresql-pvc-0"].status.phase = "Pending"
        _, reused = _ensure(fake, _layout())
        assert not reused

    def test_recreate_policy_ignores_matching_marker(self, fake):
        fake.add_set("postgresql", 3, _layout())
        _, reused = _ensure(fake, _layout(), on_create="recreate")
        assert not reused
        assert len(fake.deleted) == 3

    def test_other_prefixes_untouched(self, fake):
        fake.add_set("pg-big", 2, _layout())
        fake.add("postgresql-pvc-x")      # not <prefix>-pvc-<n>
        _ensure(fake, _layout())
        assert fake.deleted == []
        assert "pg-big-pvc-0" in fake.pvcs and "postgresql-pvc-x" in fake.pvcs

    def test_busy_when_claimed_by_running_suite(self, fake):
        other = _owner("other", "uid-other")
        fake.add_set("postgresql", 3, _layout(), claimed_by=other)
        fake.suites[("sherlockpgbenchsuites", "other")] = {
            "metadata": {"uid": "uid-other"}, "status": {"phase": "Running"}}
        with pytest.raises(volumes.VolumesBusy):
            _ensure(fake, _layout())
        assert fake.deleted == []

    @pytest.mark.parametrize("other_suite", [
        None,                                                     # deleted
        {"metadata": {"uid": "uid-newer"}, "status": {"phase": "Running"}},  # same name, new suite
        {"metadata": {"uid": "uid-other"}, "status": {"phase": "Completed"}},
    ])
    def test_not_busy_when_claimer_finished_or_gone(self, fake, other_suite):
        fake.add_set("postgresql", 3, _layout(), claimed_by=_owner("other", "uid-other"))
        if other_suite:
            fake.suites[("sherlockpgbenchsuites", "other")] = other_suite
        _, reused = _ensure(fake, _layout())
        assert reused


class TestMarkers:
    def test_mark_and_clear(self, fake):
        names, _ = _ensure(fake, _layout())
        assert not volumes.is_prepared(NS, names, _layout())
        volumes.mark_prepared(NS, names, _layout())
        assert volumes.is_prepared(NS, names, _layout())
        assert not volumes.is_prepared(NS, names, _layout(scale=20))
        volumes.clear_marker(NS, names)
        assert not volumes.is_prepared(NS, names, _layout())

    def test_password_not_stored_in_clear(self):
        assert "pw" not in json.dumps(_layout()).replace("passwordHash", "")

    def test_delete_claimed_only_deletes_own_pvcs(self, fake):
        fake.add_set("postgresql", 2, _layout(), claimed_by=_owner("a", "uid-a"))
        fake.add("postgresql-pvc-2", annotations=volumes._claim_annotations(_owner("b", "uid-b")))
        deleted = volumes.delete_claimed("postgresql", NS, "uid-a")
        assert sorted(deleted) == ["postgresql-pvc-0", "postgresql-pvc-1"]
        assert "postgresql-pvc-2" in fake.pvcs


class TestPrepareDecision:
    """base._prepare_databases skips or runs prepare based on the markers."""

    @pytest.fixture
    def prep(self, fake, monkeypatch):
        jobs = []
        monkeypatch.setattr(base.k8s, "create_job", lambda **kw: jobs.append(kw["name"]))
        monkeypatch.setattr(base.k8s, "get_job_state", lambda name, ns: "Complete")
        deployments = [{"name": f"postgresql-{j}", "node": "w", "pvc": f"postgresql-pvc-{j}",
                        "ip": "10.0.0.1"} for j in range(3)]
        for d in deployments:
            fake.add(d["pvc"])

        def run(scale, volume_status=None):
            jobs.clear()
            ok = base._prepare_databases(
                suite_name="s", namespace=NS, spec={}, run_name=f"r{scale}",
                deployments=deployments, params={"scaleFactor": scale},
                volume_status={"layout": _layout()} if volume_status is None else volume_status,
                build_prepare_command=lambda params, ip: ["prepare"],
                benchmark_image="img", suite_kind=KIND, run_key=f"k{scale}",
            )
            return ok, list(jobs)
        return run

    def test_prepares_then_marks_then_skips(self, fake, prep):
        ok, jobs = prep(10)
        assert ok and len(jobs) == 3
        assert fake.marker("postgresql-pvc-0")["dataset"] == {"scaleFactor": 10}
        ok, jobs = prep(10)
        assert ok and jobs == []

    def test_new_scale_factor_reprepares(self, fake, prep):
        prep(10)
        ok, jobs = prep(20)
        assert ok and len(jobs) == 3
        assert fake.marker("postgresql-pvc-2")["dataset"] == {"scaleFactor": 20}

    def test_failed_prepare_leaves_no_marker(self, fake, prep, monkeypatch):
        prep(10)
        monkeypatch.setattr(base.k8s, "get_job_state", lambda name, ns: "Failed")
        monkeypatch.setattr(base.k8s, "get_job_pod_logs", lambda name, ns: "")
        ok, _ = prep(20)
        assert not ok
        assert fake.marker("postgresql-pvc-0") is None

    def test_suite_without_volume_status_always_prepares(self, fake, prep):
        ok, jobs = prep(10, volume_status={})
        assert ok and len(jobs) == 3
        assert fake.marker("postgresql-pvc-0") is None
