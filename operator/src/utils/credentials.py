"""
utils/credentials.py — Database credential resolution.

Reads DB credentials from k8s Secrets rather than hardcoding them.
Each Suite CRD references a Secret by name; this module resolves it.

The Secret format is simple — one key per credential:
  apiVersion: v1
  kind: Secret
  metadata:
    name: sherlock-pg-creds
    namespace: sherlock
  type: Opaque
  stringData:
    username: sherlock
    password: mysecretpassword
    dbName:   sherlock
    # MSSQL only:
    saPassword: Sherlock1!

If no credentialsSecret is specified in the Suite spec, sensible defaults
are used for development/testing. A warning is logged in that case.

CRD field location: spec.database.credentialsSecret (string, name of Secret)
"""

import logging
import base64
from typing import Optional

from kubernetes import client
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)

# ── Defaults (dev/test only — always warn when used) ─────────────────────────
_DEFAULTS = {
    "postgresql": {
        "username":   "sherlock",
        "password":   "sherlock",
        "dbName":     "sherlock",
    },
    "mysql": {
        "username":   "sherlock",
        "password":   "sherlock",
        "dbName":     "sherlock",
    },
    "mssql": {
        "username":   "sa",
        "password":   "Sherlock1!",
        "dbName":     "tpcc",
        "saPassword": "Sherlock1!",
    },
    "mongodb": {
        "username":   "sherlock",
        "password":   "sherlock",
        "dbName":     "sherlock",
    },
}


def resolve(
    db_type: str,
    namespace: str,
    secret_name: Optional[str],
) -> dict:
    """
    Resolve DB credentials for a Suite run.

    If secret_name is provided, reads from the named k8s Secret in the
    same namespace as the Suite CR.

    If secret_name is None or empty, falls back to hardcoded defaults
    and logs a warning — this is only acceptable for dev/test.

    Returns a dict with keys: username, password, dbName, saPassword (mssql only).
    """
    if not secret_name:
        logger.warning(
            f"No credentialsSecret specified for {db_type} suite in {namespace}. "
            f"Using default credentials — DO NOT use in production."
        )
        return dict(_DEFAULTS.get(db_type, _DEFAULTS["postgresql"]))

    return _read_secret(secret_name, namespace, db_type)


def _read_secret(name: str, namespace: str, db_type: str) -> dict:
    """
    Read a k8s Secret and return credential fields.
    Raises kopf.PermanentError if the Secret doesn't exist or is missing
    required keys — this is a configuration error, not a transient failure.
    """
    import kopf

    v1 = client.CoreV1Api()
    try:
        secret = v1.read_namespaced_secret(name, namespace)
    except ApiException as e:
        if e.status == 404:
            raise kopf.PermanentError(
                f"credentialsSecret '{name}' not found in namespace '{namespace}'. "
                f"Create it with: kubectl create secret generic {name} "
                f"--from-literal=username=<user> --from-literal=password=<pass> "
                f"--from-literal=dbName=<db> -n {namespace}"
            )
        raise

    # Secret data is base64-encoded; kubernetes client returns it as bytes
    raw = secret.data or {}
    decoded = {}
    for k, v in raw.items():
        if isinstance(v, bytes):
            decoded[k] = base64.b64decode(v).decode("utf-8")
        else:
            # Already decoded (stringData path)
            decoded[k] = v

    # Validate required keys
    required = ["username", "password", "dbName"]
    if db_type == "mssql":
        required.append("saPassword")

    missing = [k for k in required if k not in decoded]
    if missing:
        raise kopf.PermanentError(
            f"credentialsSecret '{name}' is missing required keys: {missing}. "
            f"Required keys for {db_type}: {required}"
        )

    logger.info(f"Resolved credentials from Secret '{name}' for {db_type}")
    return {k: decoded[k] for k in required}


def make_example_secret(
    name: str,
    namespace: str,
    db_type: str,
) -> str:
    """
    Return a YAML string for an example Secret manifest.
    Useful for error messages and documentation.
    """
    defaults = _DEFAULTS.get(db_type, _DEFAULTS["postgresql"])
    lines = [
        f"apiVersion: v1",
        f"kind: Secret",
        f"metadata:",
        f"  name: {name}",
        f"  namespace: {namespace}",
        f"type: Opaque",
        f"stringData:",
    ]
    for k, v in defaults.items():
        lines.append(f"  {k}: {v}   # change this")
    return "\n".join(lines)
