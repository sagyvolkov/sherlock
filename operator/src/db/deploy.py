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
import re
from typing import Optional

from src.utils import k8s, volumes

logger = logging.getLogger(__name__)

# ── Image registry ────────────────────────────────────────────────────────────
# These are the current images used in sherlock v1 containers.
# v2 will update these to newer versions in a later milestone.
DB_IMAGES = {
    "postgresql": "postgres:11.7",
    "mysql":      "mysql:8.0",
    "mssql":      "mcr.microsoft.com/mssql/rhel/server:2019-latest",
    # Pinned: the data files on reused volumes must match the server version,
    # and the benchmark image's YCSB 0.17 / mongo 4.4 shell are old clients
    # (fully qualified: the cluster resolves short names via a mirror that
    # does not carry mongo)
    "mongodb":    "docker.io/library/mongo:7.0",
}

DB_PORTS = {
    "postgresql": 5432,
    "mysql":      3306,
    "mssql":      1433,
    "mongodb":    27017,
}

# ConfigMaps mounted into the database pods
DB_CONFIG_MOUNTS = {
    # mysql:8.0 reads /etc/my.cnf, which includes /etc/mysql/conf.d/*.cnf
    "mysql": [{"configmap": "mysql-custom-config", "mount_path": "/etc/mysql/conf.d"}],
    # Init script that creates the benchmark user in the benchmark database
    # (MONGO_INITDB_ROOT_* only creates a root user in admin); run by the
    # image on first start of an empty data directory
    "mongodb": [{"configmap": "prep-ycsb", "mount_path": "/docker-entrypoint-initdb.d"}],
}

# Capabilities each database container needs. restricted-v2 drops all and
# allows adding only NET_BIND_SERVICE.
DB_CAPABILITIES = {
    # /opt/mssql/bin/sqlservr has a file capability; exec fails with
    # "Operation not permitted" (exit 126) without it
    "mssql": ["NET_BIND_SERVICE"],
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
    owner: dict,
    suite_kind: str,
    name_prefix: Optional[str] = None,
    on_create: str = "reuse",
    dataset: Optional[dict] = None,
    credentials_secret: str = "",
) -> tuple[list[dict], dict]:
    """
    Deploy all database instances for a Suite run.

    Creates (instances_per_node × len(worker_nodes)) database pods,
    spreading them round-robin across worker nodes — the same pattern
    as the original bash script:
        node_number = j % NUMBER_OF_WORKERS
        node_name = worker_node_array[node_number]

    Deployments, Services and PVCs are named after name_prefix (default: the
    database type), not the suite, so a later suite can reuse the PVCs and
    the data prepared on them; see utils/volumes.py. dataset is the dataset
    params of the first run, part of the layout the PVCs must match.
    Deployments and Services are owned by the suite (owner); PVCs are not.
    Passwords reach the database containers only through credentials_secret
    (secretKeyRef env vars); db_password is used here only for the volume
    layout's password hash.

    Returns (deployments, volumes):
        deployments: [{"name": "postgresql-0", "node": "worker1",
                       "pvc": "postgresql-pvc-0", "index": 0}, ...]
        volumes: {"namePrefix", "pvcNames", "layout", "reused"}
    Raises volumes.VolumesBusy if a running suite is using the same prefix.
    """
    if db_type not in DB_IMAGES:
        raise ValueError(f"Unsupported database type: {db_type}. "
                         f"Supported: {list(DB_IMAGES.keys())}")

    k8s.ensure_namespace(namespace)

    total = instances_per_node * len(worker_nodes)
    prefix = name_prefix or db_type
    layout = volumes.make_layout(
        suite_kind=suite_kind,
        total=total,
        instances_per_node=instances_per_node,
        workers=len(worker_nodes),
        storage_class=storage_class,
        storage_size=storage_size,
        dataset=dataset,
        database={
            "type":     db_type,
            "image":    DB_IMAGES[db_type],
            "username": db_username,
            "password": db_password,
            "dbName":   db_name,
        },
    )

    # ── Pre-flight: database-specific ConfigMaps / Secrets ───────────────────
    if db_type == "mysql":
        _create_mysql_configmap(namespace)
    if db_type == "mongodb":
        _create_mongodb_configmap(namespace, db_name)

    # ── Volumes ───────────────────────────────────────────────────────────────
    # Database pods left by an earlier suite (or an earlier attempt of this
    # handler) still mount the PVCs; remove them before the PVCs are reused
    # or deleted.
    volumes.check_available(prefix, namespace, owner)
    _remove_databases(prefix, namespace)
    pvc_names, reused = volumes.ensure_volume_set(
        prefix=prefix,
        namespace=namespace,
        layout=layout,
        owner=owner,
        on_create=on_create,
        labels={"sherlock.io/suite": suite_name, "sherlock.io/role": "database-pvc"},
    )

    # ── Deploy database pods ──────────────────────────────────────────────────
    deployments = []
    for j in range(total):
        node_index = j % len(worker_nodes)
        node_name = worker_nodes[node_index]
        pod_name = f"{prefix}-{j}"
        pvc_name = pvc_names[j]

        env_vars = _build_env_vars(db_type, db_username, db_name, credentials_secret)

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
            config_mounts=DB_CONFIG_MOUNTS.get(db_type),
            add_capabilities=DB_CAPABILITIES.get(db_type),
            labels={
                "app": pod_name,
                "sherlock.io/suite": suite_name,
                "sherlock.io/role": "database",
                "sherlock.io/db-type": db_type,
                "sherlock.io/db-index": str(j),
            },
            owner=owner,
        )

        k8s.create_service(
            name=pod_name,
            namespace=namespace,
            port=DB_PORTS[db_type],
            labels={"sherlock.io/suite": suite_name},
            owner=owner,
        )

        deployments.append({
            "name":  pod_name,
            "node":  node_name,
            "pvc":   pvc_name,
            "index": j,
        })
        logger.info(f"Deployed {db_type} instance {j} on {node_name}")

    volume_status = {
        "namePrefix": prefix,
        "pvcNames":   pvc_names,
        "layout":     layout,
        "reused":     reused,
    }
    return deployments, volume_status


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
    namespace: str,
    owner_uid: str,
    volume_status: dict,
    on_suite_delete: str = "retain",
):
    """
    Called when a suite is deleted. Replaces delete_databases bash script.

    The suite's Deployments and Services are garbage-collected through their
    ownerReferences. PVCs are kept (on_suite_delete=retain) so a later suite
    can reuse the prepared data, or deleted (delete) if this suite is still
    the one that claims them.
    """
    prefix = (volume_status or {}).get("namePrefix")
    if not prefix or on_suite_delete != "delete":
        return
    deleted = volumes.delete_claimed(prefix, namespace, owner_uid)
    logger.info(f"Deleted {len(deleted)} PVCs of volume set {prefix}")


# ── Private helpers ───────────────────────────────────────────────────────────

def _remove_databases(prefix: str, namespace: str):
    """
    Delete all Deployments and Services named <prefix>-<n> and wait until
    their pods are gone, so the PVCs are free to be reused or deleted.

    They belong to an earlier suite that has finished (check_available ran
    first) or to an earlier attempt of this suite's create handler. Taking
    over another suite's objects is not an option: their ownerReference
    would delete them when that suite is deleted.
    """
    pattern = re.compile(rf"^{re.escape(prefix)}-\d+$")
    for svc in k8s.list_services(namespace):
        if pattern.match(svc.metadata.name):
            k8s.delete_service(svc.metadata.name, namespace)
    for dep in k8s.list_deployments(namespace):
        if pattern.match(dep.metadata.name):
            logger.info(f"Removing existing database {dep.metadata.name}")
            k8s.delete_deployment(dep.metadata.name, namespace, wait=True)


def _build_env_vars(
    db_type: str,
    username: str,
    db_name: str,
    secret: str,
) -> dict:
    """
    Build the environment variable dict for a database container.
    Mirrors the env: sections in the original create_databases bash functions.
    Passwords are secretKeyRefs into the credentials Secret.
    """
    def ref(key):
        return {"secret": secret, "key": key}

    if db_type == "postgresql":
        return {
            "POSTGRES_PASSWORD": ref("password"),
            "POSTGRES_USER":     username,
            "POSTGRES_DB":       db_name,
            "PGDATA":            "/var/lib/postgresql/data/pgdata",
        }
    elif db_type == "mysql":
        return {
            "MYSQL_ROOT_PASSWORD": ref("password"),
            "MYSQL_DATABASE":      db_name,
            "MYSQL_USER":          username,
            "MYSQL_PASSWORD":      ref("password"),
        }
    elif db_type == "mssql":
        return {
            "MSSQL_PID":         "Developer",
            "ACCEPT_EULA":       "Y",
            "MSSQL_SA_PASSWORD": ref("password"),
        }
    elif db_type == "mongodb":
        return {
            "MONGO_INITDB_ROOT_PASSWORD": ref("password"),
            "MONGO_INITDB_ROOT_USERNAME": username,
            "MONGO_INITDB_DATABASE":      db_name,
            # read by the prep-ycsb init script
            "SHERLOCK_DB_USER":           username,
            "SHERLOCK_DB_PASSWORD":       ref("password"),
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


def _create_mongodb_configmap(namespace: str, db_name: str):
    """
    Create the MongoDB init script ConfigMap.
    Replaces create_mongodb_config_map() in create_databases.
    Creates the benchmark user with readWrite on the benchmark db; the user
    name and password come from the container environment (the
    credentials Secret), so the ConfigMap holds no credentials.
    """
    k8s.create_configmap(
        name="prep-ycsb",
        namespace=namespace,
        data={
            "ensure-users.js": "\n".join([
                "db.createUser({",
                "  user: process.env.SHERLOCK_DB_USER,",
                "  pwd: process.env.SHERLOCK_DB_PASSWORD,",
                "  roles: [{",
                "    role: 'readWrite',",
                f"    db: '{db_name}'",
                "  }]",
                "});",
            ])
        },
        labels={"sherlock.io/role": "db-config"},
    )
