"""
volumes.py — PVC sets that outlive a suite and are reused by later suites.

Loading benchmark data can take hours (HammerDB) and the data must be larger
than RAM, so database PVCs are kept after a suite and picked up again by the
next suite that asks for exactly the same layout.

A volume set is the N PVCs named <prefix>-pvc-0 .. <prefix>-pvc-(N-1).
The prefix comes from spec.<database|storage>.volumes.namePrefix and defaults
to the database type (or "fio"), not the suite name, so a new suite finds the
volumes an earlier suite left behind.

Each PVC carries a marker annotation with the layout its data was prepared
for: suite kind, total count, instancesPerNode, worker count, storage class,
size, volume mode and the dataset params (pgbench scaleFactor, hammerdb
warehouses, ...). The marker is written only after a successful prepare and
removed before a new prepare starts, so a PVC with a marker always holds
complete data for that layout. For databases the layout also covers the
database image and credentials, because a database started on an existing
data directory ignores its init settings (e.g. POSTGRES_PASSWORD).

A set is reused only if all N PVCs exist, nothing else has the prefix, every
PVC is Bound and every marker matches. Anything else deletes the whole set
and creates N fresh PVCs; a partly matching set is never reused.

PVCs have no ownerReference (they must survive the suite). The suite that is
using a set is recorded in claim annotations, which stop two active suites
from sharing a set and limit onSuiteDelete=delete to the suite's own PVCs.
"""

import hashlib
import json
import logging
import re
from typing import Optional

from src.utils import k8s

logger = logging.getLogger(__name__)

MARKER_ANNOTATION = "sherlock.io/prepared-layout"
CLAIM_KIND        = "sherlock.io/claimed-by-kind"
CLAIM_NAME        = "sherlock.io/claimed-by-name"
CLAIM_UID         = "sherlock.io/claimed-by-uid"
LABEL_VOLUME_SET  = "sherlock.io/volume-set"

# Suite phases after which a suite no longer uses its volumes
TERMINAL_PHASES = ("Completed", "Degraded", "Failed")

# Run params that determine what prepare writes to the volumes
DATASET_PARAMS = {
    "SherlockPgbenchSuite":  ("scaleFactor",),
    "SherlockHammerDBSuite": ("warehouses",),
    "SherlockSysbenchSuite": ("tables", "tableSize"),
    "SherlockYCSBSuite":     ("recordCount",),
    "SherlockFioSuite":      (),
}

PLURALS = {
    "SherlockPgbenchSuite":  "sherlockpgbenchsuites",
    "SherlockHammerDBSuite": "sherlockhammerdbsuites",
    "SherlockSysbenchSuite": "sherlocksysbenchsuites",
    "SherlockYCSBSuite":     "sherlockycsbbenchsuites",
    "SherlockFioSuite":      "sherlockfiosuites",
}


class VolumesBusy(Exception):
    """The volume set is claimed by another suite that is still running."""


def settings(block: dict, default_prefix: str) -> dict:
    """Read a volumes block (spec.database.volumes / spec.storage.volumes)."""
    volumes = (block or {}).get("volumes") or {}
    return {
        "namePrefix":    volumes.get("namePrefix") or default_prefix,
        "onSuiteDelete": volumes.get("onSuiteDelete", "retain"),
        "onCreate":      volumes.get("onCreate", "reuse"),
    }


def pvc_name(prefix: str, index: int) -> str:
    return f"{prefix}-pvc-{index}"


def dataset_params(suite_kind: str, params: dict) -> dict:
    """The part of a run's params that the prepared data depends on."""
    return {k: params.get(k) for k in DATASET_PARAMS.get(suite_kind, ())}


def make_layout(
    suite_kind: str,
    total: int,
    instances_per_node: int,
    workers: int,
    storage_class: str,
    storage_size: str,
    volume_mode: str = "Filesystem",
    dataset: Optional[dict] = None,
    database: Optional[dict] = None,
) -> dict:
    """
    Everything a set of prepared volumes must match to be reused.
    database: {"type", "image", "username", "password", "dbName"}; the
    password is stored only as a short hash.
    """
    layout = {
        "kind":             suite_kind,
        "count":            total,
        "instancesPerNode": instances_per_node,
        "workers":          workers,
        "storageClass":     storage_class or "",
        "storageSize":      storage_size,
        "volumeMode":       volume_mode,
        "dataset":          dataset or {},
    }
    if database:
        db = {k: v for k, v in database.items() if k != "password"}
        db["passwordHash"] = hashlib.sha256(
            (database.get("password") or "").encode()
        ).hexdigest()[:16]
        layout["database"] = db
    return layout


def ensure_volume_set(
    prefix: str,
    namespace: str,
    layout: dict,
    owner: dict,
    on_create: str = "reuse",
    labels: Optional[dict] = None,
    bound_timeout: int = 600,
) -> tuple[list[str], bool]:
    """
    Make the volume set <prefix>-pvc-0..N-1 ready for the suite `owner`.

    Returns (pvc_names, reused). reused=True means the PVCs already hold
    data prepared for `layout`; otherwise they were all created fresh.
    Raises VolumesBusy if another suite that is still running claims them.
    """
    total = layout["count"]
    names = [pvc_name(prefix, j) for j in range(total)]
    existing = check_available(prefix, namespace, owner)

    reason = _reuse_blocker(existing, names, layout) if on_create == "reuse" \
        else "onCreate=recreate"
    if reason is None:
        logger.info(
            f"Reusing volume set {prefix} ({total} PVCs prepared for "
            f"{layout['dataset'] or 'this layout'})"
        )
        claim(namespace, names, owner)
        return names, True

    if existing:
        logger.info(
            f"Recreating volume set {prefix}: {reason}. "
            f"Deleting {len(existing)} PVCs: {sorted(existing)}"
        )
        for name in existing:
            k8s.delete_pvc(name, namespace)
        k8s.wait_for_pvcs_deleted(namespace, list(existing), timeout=bound_timeout)
    else:
        logger.info(f"Creating volume set {prefix} ({total} PVCs)")

    for j, name in enumerate(names):
        k8s.create_pvc(
            name=name,
            namespace=namespace,
            storage_size=layout["storageSize"],
            storage_class=layout["storageClass"],
            volume_mode=layout["volumeMode"],
            labels={**(labels or {}), LABEL_VOLUME_SET: prefix,
                    "sherlock.io/volume-index": str(j)},
            annotations=_claim_annotations(owner),
        )
    k8s.wait_for_pvcs_bound(namespace, names, timeout=bound_timeout)
    return names, False


def check_available(prefix: str, namespace: str, owner: dict) -> dict:
    """
    Raise VolumesBusy if a different suite that is still running claims the
    set; otherwise return the set's existing PVCs by name.
    """
    existing = _volume_set_pvcs(prefix, namespace)
    my_uid = owner["metadata"]["uid"]
    for pvc in existing.values():
        ann = pvc.metadata.annotations or {}
        uid, kind, name = ann.get(CLAIM_UID), ann.get(CLAIM_KIND), ann.get(CLAIM_NAME)
        if not uid or uid == my_uid or kind not in PLURALS:
            continue
        other = k8s.get_custom_resource(
            "sherlock.io", "v1alpha1", PLURALS[kind], name, pvc.metadata.namespace,
        )
        if not other or other["metadata"].get("uid") != uid:
            continue  # claiming suite is gone
        phase = (other.get("status") or {}).get("phase")
        if phase not in TERMINAL_PHASES:
            raise VolumesBusy(
                f"volume set {prefix} is in use by {kind} {name} (phase {phase})"
            )
    return existing


def read_markers(namespace: str, names: list[str]) -> list[Optional[dict]]:
    """The prepared layout recorded on each PVC (None if missing/unreadable)."""
    markers = []
    for name in names:
        pvc = k8s.read_pvc(name, namespace)
        annotations = (pvc.metadata.annotations or {}) if pvc else {}
        markers.append(_parse_marker(annotations.get(MARKER_ANNOTATION)))
    return markers


def is_prepared(namespace: str, names: list[str], layout: dict) -> bool:
    """True if every PVC's marker matches layout, i.e. prepare can be skipped."""
    return bool(names) and all(m == layout for m in read_markers(namespace, names))


def mark_prepared(namespace: str, names: list[str], layout: dict):
    """Record that the PVCs now hold data for layout. Call after a successful prepare."""
    value = json.dumps(layout, sort_keys=True)
    for name in names:
        k8s.patch_pvc_metadata(name, namespace, annotations={MARKER_ANNOTATION: value})


def clear_marker(namespace: str, names: list[str]):
    """Forget the prepared layout. Call before prepare starts overwriting the data."""
    for name in names:
        k8s.patch_pvc_metadata(name, namespace, annotations={MARKER_ANNOTATION: None})


def claim(namespace: str, names: list[str], owner: dict):
    """Record owner as the suite using these PVCs."""
    for name in names:
        k8s.patch_pvc_metadata(
            name, namespace,
            labels={"sherlock.io/suite": owner["metadata"]["name"]},
            annotations=_claim_annotations(owner),
        )


def delete_claimed(prefix: str, namespace: str, owner_uid: str) -> list[str]:
    """
    Delete the PVCs of the set that are claimed by the suite with owner_uid.
    PVCs since claimed by a later suite are left alone. Does not wait: a PVC
    still mounted stays Terminating until its pod is gone.
    """
    deleted = []
    for name, pvc in _volume_set_pvcs(prefix, namespace).items():
        if (pvc.metadata.annotations or {}).get(CLAIM_UID) == owner_uid:
            k8s.delete_pvc(name, namespace)
            deleted.append(name)
    return deleted


# ── Private helpers ───────────────────────────────────────────────────────────

def _volume_set_pvcs(prefix: str, namespace: str) -> dict:
    """All PVCs named <prefix>-pvc-<n>, including ones beyond the expected count."""
    pattern = re.compile(rf"^{re.escape(prefix)}-pvc-\d+$")
    return {
        pvc.metadata.name: pvc
        for pvc in k8s.list_pvcs(namespace)
        if pattern.match(pvc.metadata.name)
    }


def _reuse_blocker(existing: dict, names: list[str], layout: dict) -> Optional[str]:
    """Why the existing PVCs can't be reused, or None if they can."""
    if not existing:
        return "no existing PVCs"
    missing = [n for n in names if n not in existing]
    if missing:
        return f"missing {missing}"
    extra = sorted(set(existing) - set(names))
    if extra:
        return f"{len(extra)} PVCs beyond the expected {len(names)}: {extra}"
    for name in names:
        pvc = existing[name]
        if pvc.metadata.deletion_timestamp:
            return f"{name} is being deleted"
        if (pvc.status.phase if pvc.status else None) != "Bound":
            return f"{name} is not Bound"
        marker = _parse_marker((pvc.metadata.annotations or {}).get(MARKER_ANNOTATION))
        if marker is None:
            return f"{name} has no prepared-data marker"
        if marker != layout:
            diff = ", ".join(f"{k} {marker.get(k)} -> {v}"
                             for k, v in layout.items() if marker.get(k) != v)
            return f"{name} was prepared for a different layout ({diff})"
    return None


def _claim_annotations(owner: dict) -> dict:
    meta = owner["metadata"]
    return {CLAIM_KIND: owner["kind"], CLAIM_NAME: meta["name"], CLAIM_UID: meta["uid"]}


def _parse_marker(value: Optional[str]) -> Optional[dict]:
    if not value:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None
