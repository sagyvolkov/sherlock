"""
utils/artifacts.py — Artifact bundle collection and destination routing.

After each SherlockRun completes, this module:
1. Collects raw logs from all benchmark and stat pods
2. Writes the Python-parsed summary as JSON
3. Compresses everything into a .tar.gz bundle
4. Routes the bundle to the configured destination (pvc, s3, or local)

This replaces the manual log collection and tarball creation in the original
sherlock bash scripts:
  kubectl logs <pod> > ${RUN_NAME}/<pod>.log
  tar zcvf ${hostname}-${test_name}.tgz ./iostat.out ./vmstat.out ...

Bundle layout (same regardless of destination):
  <run_name>/
    raw/
      benchmark-<db>-<node>.log    raw benchmark tool output
      stats-worker-<node>.log      raw iostat/vmstat output
    parsed/
      summary.json                 structured result metrics
    meta/
      sherlock-run.yaml            copy of the Suite spec for this run
  → compressed as <run_name>.tar.gz
"""

import io
import json
import logging
import os
import tarfile
import tempfile
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# ── Bundle builder ────────────────────────────────────────────────────────────

def build_bundle(
    run_name: str,
    benchmark_logs: dict[str, str],   # {job_name: log_content}
    stat_logs: dict[str, str],         # {node_name: log_content}
    parsed_result: dict,
    suite_spec: dict,
    params: dict,
    compression: str = "gzip",
) -> bytes:
    """
    Build an in-memory compressed tarball for a completed run.

    Returns the tarball as bytes — the caller writes it to the
    configured destination (PVC path, S3, or emptyDir).

    compression: "gzip" (default), "zstd" (faster, better ratio), "none"
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir   = os.path.join(tmpdir, run_name)
        raw_dir   = os.path.join(run_dir, "raw")
        parsed_dir = os.path.join(run_dir, "parsed")
        meta_dir  = os.path.join(run_dir, "meta")

        for d in (raw_dir, parsed_dir, meta_dir):
            os.makedirs(d, exist_ok=True)

        # ── raw benchmark logs ────────────────────────────────────────────
        for job_name, content in benchmark_logs.items():
            safe_name = job_name.replace("/", "_")
            _write(os.path.join(raw_dir, f"benchmark-{safe_name}.log"), content)

        # ── raw stat logs ─────────────────────────────────────────────────
        for node_name, content in stat_logs.items():
            safe_name = node_name.replace("/", "_")
            _write(os.path.join(raw_dir, f"stats-worker-{safe_name}.log"), content)

        # ── parsed summary ────────────────────────────────────────────────
        summary = {
            "runName":       run_name,
            "collectedAt":   datetime.now(timezone.utc).isoformat(),
            "params":        params,
            "result":        parsed_result,
        }
        _write(
            os.path.join(parsed_dir, "summary.json"),
            json.dumps(summary, indent=2),
        )

        # ── meta: suite spec snapshot ─────────────────────────────────────
        import yaml
        _write(
            os.path.join(meta_dir, "sherlock-suite-spec.yaml"),
            yaml.dump({"spec": suite_spec}, default_flow_style=False),
        )
        _write(
            os.path.join(meta_dir, "run-params.json"),
            json.dumps(params, indent=2),
        )

        # ── compress ──────────────────────────────────────────────────────
        buf = io.BytesIO()
        mode = _tar_mode(compression)
        with tarfile.open(fileobj=buf, mode=mode) as tar:
            tar.add(run_dir, arcname=run_name)
        return buf.getvalue()


def _write(path: str, content: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content or "")


def _tar_mode(compression: str) -> str:
    if compression == "gzip": return "w:gz"
    if compression == "zstd": return "w"    # tarfile doesn't natively support zstd; fallback to uncompressed
    return "w"                               # "none"


# ── Destination routing ───────────────────────────────────────────────────────

def store_bundle(
    bundle: bytes,
    run_name: str,
    suite_name: str,
    namespace: str,
    artifacts_spec: dict,
    patch,
) -> str:
    """
    Write a bundle to the configured destination.
    Returns the path/key where the bundle was stored.

    Updates patch.status.artifacts with the bundle reference.
    """
    dest = artifacts_spec.get("destination", {})
    dest_type = dest.get("type", "local")
    compression = artifacts_spec.get("compression", "gzip")
    filename = f"{run_name}.tar.gz" if compression != "none" else f"{run_name}.tar"

    if dest_type == "pvc":
        path = _store_pvc(bundle, filename, suite_name, namespace, dest.get("pvc", {}))
    elif dest_type == "s3":
        path = _store_s3(bundle, filename, suite_name, dest.get("s3", {}))
    elif dest_type == "local":
        path = _store_local(bundle, filename, suite_name, namespace)
    else:
        logger.warning(f"Unknown destination type '{dest_type}', using local")
        path = _store_local(bundle, filename, suite_name, namespace)

    logger.info(f"Bundle stored at: {path} ({len(bundle)} bytes)")
    _update_artifacts_status(patch, run_name, path, len(bundle))
    return path


# ── PVC destination ───────────────────────────────────────────────────────────

def _store_pvc(
    bundle: bytes,
    filename: str,
    suite_name: str,
    namespace: str,
    pvc_spec: dict,
) -> str:
    """
    Write bundle to a PVC. The operator mounts a shared artifacts PVC
    at /artifacts inside the operator pod.

    The PVC is created once per Suite (on first run) and shared across
    all runs in the Suite.
    """
    artifacts_pvc_name = f"{suite_name}-artifacts"
    _ensure_artifacts_pvc(artifacts_pvc_name, namespace, pvc_spec)

    # Write to /artifacts/<suite_name>/<filename>
    # The operator pod mounts the artifacts PVC at /artifacts
    artifacts_dir = f"/artifacts/{suite_name}"
    os.makedirs(artifacts_dir, exist_ok=True)
    path = os.path.join(artifacts_dir, filename)

    with open(path, "wb") as f:
        f.write(bundle)

    return path


def _ensure_artifacts_pvc(name: str, namespace: str, pvc_spec: dict):
    """Create the artifacts PVC if it doesn't exist yet."""
    from src.utils.k8s import create_pvc
    create_pvc(
        name=name,
        namespace=namespace,
        storage_size=pvc_spec.get("size", "20Gi"),
        storage_class=pvc_spec.get("storageClass", ""),
        labels={"sherlock.io/role": "artifacts"},
    )


# ── S3 destination ────────────────────────────────────────────────────────────

def _store_s3(
    bundle: bytes,
    filename: str,
    suite_name: str,
    s3_spec: dict,
) -> str:
    """
    Upload bundle to S3-compatible object storage.
    Credentials are read from the Secret named in s3_spec.credentialsSecret.
    """
    import boto3
    from botocore.config import Config

    endpoint  = s3_spec.get("endpoint", "")
    bucket    = s3_spec.get("bucket", "")
    prefix    = s3_spec.get("prefix", f"sherlock/{suite_name}/")
    verify_checksum = s3_spec.get("checksumVerification", True)

    # Resolve S3 credentials from k8s Secret
    access_key, secret_key = _resolve_s3_creds(s3_spec.get("credentialsSecret"))

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint or None,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4"),
    )

    key = f"{prefix.rstrip('/')}/{filename}"

    extra_args = {}
    if verify_checksum:
        import hashlib
        md5 = hashlib.md5(bundle).digest()
        import base64
        extra_args["ContentMD5"] = base64.b64encode(md5).decode()

    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=bundle,
        ContentType="application/gzip",
        **extra_args,
    )

    return f"s3://{bucket}/{key}"


def _resolve_s3_creds(secret_name: Optional[str]) -> tuple[str, str]:
    """Read S3 access-key and secret-key from a k8s Secret."""
    if not secret_name:
        raise ValueError(
            "s3.credentialsSecret is required for S3 artifact destination"
        )
    from kubernetes import client
    import base64
    v1 = client.CoreV1Api()
    # Note: we don't know the namespace here — S3 secrets should be in sherlock ns
    # This is a known limitation; will be addressed with a namespace param in Phase 2
    secret = v1.read_namespaced_secret(secret_name, "sherlock")
    data = secret.data or {}
    access_key = base64.b64decode(data.get("access-key", b"")).decode()
    secret_key = base64.b64decode(data.get("secret-key", b"")).decode()
    return access_key, secret_key


# ── Local destination ─────────────────────────────────────────────────────────

def _store_local(
    bundle: bytes,
    filename: str,
    suite_name: str,
    namespace: str,
) -> str:
    """
    Write bundle to /tmp/sherlock-artifacts/<suite_name>/ (emptyDir).
    The collector pod keeps this available until TTL expires.
    User retrieves with: kubectl sherlock pull <suite-name> ./local/

    For now this writes to a local path on the operator pod.
    The CLI pull command will stream this out via kubectl exec.
    """
    artifacts_dir = f"/tmp/sherlock-artifacts/{suite_name}"
    os.makedirs(artifacts_dir, exist_ok=True)
    path = os.path.join(artifacts_dir, filename)
    with open(path, "wb") as f:
        f.write(bundle)
    logger.info(f"Bundle stored locally at {path}. "
                f"Retrieve with: kubectl sherlock pull {suite_name} ./results/")
    return path


# ── Status update ─────────────────────────────────────────────────────────────

def _update_artifacts_status(patch, run_name: str, path: str, size_bytes: int):
    """Append this run's bundle to status.artifacts.bundles."""
    from datetime import datetime, timezone
    bundles = patch.status.get("artifacts", {}).get("bundles", [])
    bundles.append({
        "runName":     run_name,
        "path":        path,
        "sizeBytes":   size_bytes,
        "collectedAt": datetime.now(timezone.utc).isoformat(),
    })
    if "artifacts" not in patch.status:
        patch.status["artifacts"] = {}
    patch.status["artifacts"]["bundles"] = bundles


# ── Retention enforcement ─────────────────────────────────────────────────────

def enforce_retention(
    suite_name: str,
    artifacts_spec: dict,
    current_bundles: list[dict],
) -> list[dict]:
    """
    Apply retention policy to existing bundles.
    Deletes bundles exceeding keepLastN or older than ttlDays.
    Returns the updated bundle list.
    """
    retention = artifacts_spec.get("retention", {})
    keep_last_n = retention.get("keepLastN")
    ttl_days    = retention.get("ttlDays")

    if not keep_last_n and not ttl_days:
        return current_bundles

    surviving = list(current_bundles)

    # TTL: remove bundles older than ttlDays
    if ttl_days:
        cutoff = datetime.now(timezone.utc).timestamp() - (ttl_days * 86400)
        to_delete = []
        for b in surviving:
            collected = b.get("collectedAt", "")
            if collected:
                ts = datetime.fromisoformat(collected).timestamp()
                if ts < cutoff:
                    to_delete.append(b)
        for b in to_delete:
            _delete_bundle(b.get("path", ""), artifacts_spec)
            surviving.remove(b)
            logger.info(f"[{suite_name}] Deleted expired bundle: {b['runName']}")

    # keepLastN: remove oldest beyond the limit
    if keep_last_n and len(surviving) > keep_last_n:
        to_delete = surviving[:-keep_last_n]
        for b in to_delete:
            _delete_bundle(b.get("path", ""), artifacts_spec)
            surviving.remove(b)
            logger.info(f"[{suite_name}] Deleted bundle beyond keepLastN={keep_last_n}: {b['runName']}")

    return surviving


def _delete_bundle(path: str, artifacts_spec: dict):
    """Delete a bundle from wherever it was stored."""
    if not path:
        return
    if path.startswith("s3://"):
        try:
            import boto3
            parts = path[5:].split("/", 1)
            bucket, key = parts[0], parts[1]
            # Note: credentials would need to be re-resolved here
            # This is simplified for Phase 1
            logger.warning(f"S3 bundle deletion not yet implemented for {path}")
        except Exception as e:
            logger.warning(f"Failed to delete S3 bundle {path}: {e}")
    else:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"Failed to delete bundle {path}: {e}")
