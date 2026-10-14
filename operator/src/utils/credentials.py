"""
utils/credentials.py — Database credentials.

All suites share one Secret, sherlock-db-credentials, in the operator's
namespace (a suite can name another one in spec.database.credentialsSecret):

  kubectl create secret generic sherlock-db-credentials -n sherlock \
    --from-literal=username=sherlock --from-literal=password='Sherlock1!' \
    --from-literal=dbName=sherlock

If sherlock-db-credentials doesn't exist, the operator creates it with the
defaults below. The same password is used for every database type, so it
must meet SQL Server's complexity rules (8+ chars, upper, lower, digit,
symbol). MSSQL always uses the sa user and the tpcc database.

Database pods and benchmark Jobs read the password from the Secret
(secretKeyRef env vars); commands refer to "$SHERLOCK_DB_PASSWORD" etc.
"""

import base64
import logging
from typing import Optional

import kopf
from kubernetes import client
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)

DEFAULT_SECRET = "sherlock-db-credentials"
DEFAULTS = {"username": "sherlock", "password": "Sherlock1!", "dbName": "sherlock"}

# Client (prepare / benchmark Job) environment → key in the Secret
CLIENT_ENV = {
    "SHERLOCK_DB_USER":     "username",
    "SHERLOCK_DB_PASSWORD": "password",
    "SHERLOCK_DB_NAME":     "dbName",
}
MSSQL_CLIENT_ENV = {
    "SHERLOCK_SA_PASSWORD": "password",
    "SQLCMDPASSWORD":       "password",   # read by sqlcmd instead of -P
}


def resolve(db_type: str, namespace: str, secret_name: Optional[str]) -> dict:
    """
    Read the credentials Secret. Returns {secret, username, password, dbName}.
    The default Secret is created when missing; a named one must exist.
    """
    name = secret_name or DEFAULT_SECRET
    v1 = client.CoreV1Api()
    try:
        data = v1.read_namespaced_secret(name, namespace).data or {}
        creds = {k: base64.b64decode(v).decode() for k, v in data.items()}
    except ApiException as e:
        if e.status != 404:
            raise
        if name != DEFAULT_SECRET:
            raise kopf.PermanentError(
                f"credentialsSecret '{name}' not found in namespace '{namespace}'")
        from src.utils import k8s
        k8s.create_secret(name, namespace, DEFAULTS,
                          labels={"sherlock.io/role": "db-credentials"})
        logger.info(f"Created Secret {name} with default database credentials")
        creds = dict(DEFAULTS)

    missing = [k for k in ("username", "password", "dbName") if k not in creds]
    if missing:
        raise kopf.PermanentError(f"Secret '{name}' is missing keys: {missing}")

    result = {"secret": name, "username": creds["username"],
              "password": creds["password"], "dbName": creds["dbName"]}
    if db_type == "mssql":
        result.update(username="sa", dbName="tpcc")
    return result


def client_secret_env(secret_name: str, db_type: str) -> dict:
    """{env var: (secret, key)} for prepare and benchmark Jobs."""
    env = dict(CLIENT_ENV)
    if db_type == "mssql":
        env.update(MSSQL_CLIENT_ENV)
    return {var: (secret_name, key) for var, key in env.items()}
