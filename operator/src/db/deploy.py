"""
db/deploy.py — Database deployment and teardown.

Contains the logic to deploy and clean up each supported database type.
This replaces the create_postgresql(), create_mysql(), create_mongodb(),
create_sqlserver() functions from the original create_databases bash script,
and the corresponding delete logic from delete_databases.

All DB benchmark handlers (pgbench, sysbench, hammerdb, ycsb) call these
functions — the database deployment is the same regardless of which benchmark
tool will be run against it.

Supported databases: postgresql, mysql, mssql, mongodb
"""

import logging
from typing import Optional

from src.utils import k8s

logger = logging.getLogger(__name__)

# ── Image registry ────────────────────────────────────────────────────────────
# These are the current images used in sherlock v1 containers.
# v2 will update these to newer versions in a later milestone.
DB_IMAGES = {
    "postgresql": "postgres:11.7",
    "mysql":      "mysql:8.0",
    "mssql":      "mcr.microsoft.com/mssql/rhel/server:2019-latest",
    "mongodb":    "mongo:latest",
}

DB_PORTS = {
    "postgresql": 5432,
    "mysql":      3306,
    "mssql":      1433,
    "mongodb":    27017,
}

DB_DATA_PATHS = {
    "postgresql": "/var/lib/postgresql/data",
    "mysql":      "/var/lib/mysql",
    "mssql":      "/var/opt/mssql",
    "mongodb":    "/data/db",
}


def deploy_databases(
    suite_name: str,
    namespace: str,
    db_type: str,
    db_username: str,
    db_password: str,
    db_name: str,
    storage_class: str,
    storage_size: str,
    instances_per_node: int,
    worker_nodes: list[str],
    resources: dict,
    sa_password: Optional[str] = None,   # MSSQL only
) -> list[dict]:
    """
    Deploy all database instances for a Suite run.

    Creates (instances_per_node × len(worker_nodes)) database pods,
    spreading them round-robin across worker nodes — the same pattern
    as the original bash script:
        node_number = j % NUMBER_OF_WORKERS
        node_name = worker_node_array[node_number]

    Returns a list of deployment descriptors:
        [{"name": "postgresql-0", "node": "worker1", "pvc": "postgresql-pvc-0",
          "ip": "10.0.0.1", "index": 0}, ...]
    """
    if db_type not in DB_IMAGES:
        raise ValueError(f"Unsupported database type: {db_type}. "
                         f"Supported: {list(DB_IMAGES.keys())}")

    k8s.ensure_namespace(namespace)

    total = instances_per_node * len(worker_nodes)
    prefix = f"{suite_name}-{db_type}"
    pvc_prefix = f"{prefix}-pvc"

    # ── Pre-flight: database-specific ConfigMaps / Secrets ───────────────────
    if db_type == "mysql":
        _create_mysql_configmap(namespace)
    if db_type == "mongodb":
        _create_mongodb_configmap(namespace, db_username, db_password, db_name)
    if db_type == "mssql":
        if not sa_password:
            raise ValueError("sa_password is required for mssql deployments")
        k8s.create_secret("sqlserver", namespace, {"SA_PASSWORD": sa_password})

    # ── Create all PVCs first ─────────────────────────────────────────────────
    # Matches the original create_all_pvc() pattern:
    #   for j in 0..DB_PER_WORKER*NUMBER_OF_WORKERS-1: create_pvc
    pvc_names = []
    for j in range(total):
        pvc_name = f"{pvc_prefix}-{j}"
        labels = {
            "sherlock.io/suite": suite_name,
            "sherlock.io/role": "database-pvc",
            "sherlock.io/db-index": str(j),
        }
        k8s.create_pvc(
            name=pvc_name,
            namespace=namespace,
            storage_size=storage_size,
            storage_class=storage_class,
            labels=labels,
        )
        pvc_names.append(pvc_name)

    k8s.wait_for_pvcs_bound(namespace, pvc_names, timeout=600)

    # ── Deploy database pods ──────────────────────────────────────────────────
    deployments = []
    for j in range(total):
        node_index = j % len(worker_nodes)
        node_name = worker_nodes[node_index]
        pod_name = f"{prefix}-{j}"
        pvc_name = f"{pvc_prefix}-{j}"

        env_vars = _build_env_vars(db_type, db_username, db_password, db_name, sa_password)

        k8s.create_deployment(
            name=pod_name,
            namespace=namespace,
            image=DB_IMAGES[db_type],
            pvc_name=pvc_name,
            node_name=node_name,
            mount_path=DB_DATA_PATHS[db_type],
            env_vars=env_vars,
            port=DB_PORTS[db_type],
            resources=resources,
            labels={
                "app": pod_name,
                "sherlock.io/suite": suite_name,
                "sherlock.io/role": "database",
                "sherlock.io/db-type": db_type,
                "sherlock.io/db-index": str(j),
            },
        )

        k8s.create_service(
            name=pod_name,
            namespace=namespace,
            port=DB_PORTS[db_type],
            labels={"sherlock.io/suite": suite_name},
        )

        deployments.append({
            "name":  pod_name,
            "node":  node_name,
            "pvc":   pvc_name,
            "index": j,
        })
        logger.info(f"Deployed {db_type} instance {j} on {node_name}")

    return deployments


def wait_for_databases_ready(
    deployments: list[dict],
    namespace: str,
    timeout: int = 300,
) -> list[dict]:
    """
    Wait for all database pods to become Ready and resolve their ClusterIPs.
    Returns the deployments list with 'ip' field populated.

    Replaces:
      kubectl wait --for=condition=Ready pod -l app=<pod> --timeout=120s
      kubectl get svc -o jsonpath='{.spec.clusterIP}'
    """
    for dep in deployments:
        k8s.wait_for_deployment_ready(dep["name"], namespace, timeout=timeout)
        dep["ip"] = k8s.get_service_cluster_ip(dep["name"], namespace)
        logger.info(f"Database {dep['name']} ready at {dep['ip']}")
    return deployments


def teardown_databases(
    suite_name: str,
    namespace: str,
    db_type: str,
    total_instances: int,
    delete_pvcs: bool = False,
):
    """
    Delete database Deployments, Services, and optionally PVCs.
    Replaces delete_databases bash script.

    delete_pvcs: defaults False because data on PVCs may still be needed
    for result archiving. Set True only when fully cleaning up a Suite.
    """
    prefix = f"{suite_name}-{db_type}"
    for j in range(total_instances):
        pod_name = f"{prefix}-{j}"
        pvc_name = f"{prefix}-pvc-{j}"
        k8s.delete_deployment(pod_name, namespace)
        # Service deletion — use CoreV1Api directly
        try:
            v1 = __import__("kubernetes").client.CoreV1Api()
            v1.delete_namespaced_service(pod_name, namespace)
            logger.info(f"Deleted service {pod_name}")
        except Exception:
            pass
        if delete_pvcs:
            k8s.delete_pvc(pvc_name, namespace)


# ── Private helpers ───────────────────────────────────────────────────────────

def _build_env_vars(
    db_type: str,
    username: str,
    password: str,
    db_name: str,
    sa_password: Optional[str] = None,
) -> dict:
    """
    Build the environment variable dict for a database container.
    Mirrors the env: sections in the original create_databases bash functions.
    """
    if db_type == "postgresql":
        return {
            "POSTGRES_PASSWORD": password,
            "POSTGRES_USER":     username,
            "POSTGRES_DB":       db_name,
            "PGDATA":            "/var/lib/postgresql/data/pgdata",
        }
    elif db_type == "mysql":
        return {
            "MYSQL_ROOT_PASSWORD": password,
            "MYSQL_DATABASE":      db_name,
            "MYSQL_USER":          username,
            "MYSQL_PASSWORD":      password,
        }
    elif db_type == "mssql":
        return {
            "MSSQL_PID":   "Developer",
            "ACCEPT_EULA": "Y",
            # SA_PASSWORD comes from a Secret — expressed as a secretKeyRef
            "MSSQL_SA_PASSWORD": {
                "secret": "sqlserver",
                "key":    "SA_PASSWORD",
            },
        }
    elif db_type == "mongodb":
        return {
            "MONGO_INITDB_ROOT_PASSWORD": password,
            "MONGO_INITDB_ROOT_USERNAME": username,
            "MONGO_INITDB_DATABASE":      db_name,
        }
    else:
        raise ValueError(f"Unknown db_type: {db_type}")


def _create_mysql_configmap(namespace: str):
    """
    Create the MySQL custom config ConfigMap.
    Replaces create_mysql_config_map() in create_databases.
    Sets innodb_buffer_pool_size and other tuning params.
    """
    k8s.create_configmap(
        name="mysql-custom-config",
        namespace=namespace,
        data={
            "mysql.sherlock.cnf": "\n".join([
                "[mysqld]",
                "skip-host-cache",
                "skip-name-resolve",
                "innodb_buffer_pool_size = 1G",
                "default_authentication_plugin = mysql_native_password",
                "[mysqldump]",
                "quick",
                "quote-names",
                "max_allowed_packet = 16M",
            ])
        },
        labels={"sherlock.io/role": "db-config"},
    )


def _create_mongodb_configmap(
    namespace: str,
    username: str,
    password: str,
    db_name: str,
):
    """
    Create the MongoDB init script ConfigMap.
    Replaces create_mongodb_config_map() in create_databases.
    Creates the sherlock user with readWrite role on the benchmark db.
    """
    k8s.create_configmap(
        name="prep-ycsb",
        namespace=namespace,
        data={
            "ensure-users.js": "\n".join([
                "db.createUser({",
                f"  user: '{username}',",
                f"  pwd: '{password}',",
                "  roles: [{",
                "    role: 'readWrite',",
                f"    db: '{db_name}'",
                "  }]",
                "});",
            ])
        },
        labels={"sherlock.io/role": "db-config"},
    )
