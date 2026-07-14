"""
k8s.py — Kubernetes client helpers.

Thin wrappers around the kubernetes-client Python library.
All handlers use these instead of calling the k8s API directly,
which keeps the API surface small and makes testing easier.

Replaces the kubectl calls in the original bash scripts.
"""

import logging
import time
from typing import Optional

from kubernetes import client, config
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)

# ── Client initialisation ────────────────────────────────────────────────────

def load_config():
    """
    Load k8s config — in-cluster when running as a pod, local kubeconfig
    when running for development. Called once at operator startup.
    """
    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster k8s config")
    except config.ConfigException:
        config.load_kube_config()
        logger.info("Loaded local kubeconfig")


# ── Namespace ────────────────────────────────────────────────────────────────

def ensure_namespace(namespace: str):
    """
    Create namespace if it does not exist.
    Replaces:
      if ! kubectl get namespaces | grep -w ${NAMESPACE_NAME}; then
        kubectl create namespace ${NAMESPACE_NAME}
    """
    v1 = client.CoreV1Api()
    try:
        v1.read_namespace(namespace)
        logger.debug(f"Namespace {namespace} already exists")
    except ApiException as e:
        if e.status == 404:
            ns = client.V1Namespace(metadata=client.V1ObjectMeta(name=namespace))
            v1.create_namespace(ns)
            logger.info(f"Created namespace {namespace}")
        else:
            raise


# ── PVCs ─────────────────────────────────────────────────────────────────────

def create_pvc(
    name: str,
    namespace: str,
    storage_size: str,
    storage_class: str = "",
    volume_mode: str = "Filesystem",
    labels: Optional[dict] = None,
) -> client.V1PersistentVolumeClaim:
    """
    Create a PVC and return it.
    Replaces the create_pvc() bash function in create_databases and run_fio_job.

    volume_mode: "Filesystem" (default) or "Block" (for raw fio block tests).
    storage_class: empty string uses the cluster default StorageClass.
    """
    v1 = client.CoreV1Api()
    spec = client.V1PersistentVolumeClaimSpec(
        access_modes=["ReadWriteOnce"],
        volume_mode=volume_mode,
        resources=client.V1ResourceRequirements(
            requests={"storage": storage_size}
        ),
    )
    if storage_class:
        spec.storage_class_name = storage_class

    pvc = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels=labels or {},
        ),
        spec=spec,
    )
    try:
        result = v1.create_namespaced_persistent_volume_claim(namespace, pvc)
        logger.info(f"Created PVC {name} in {namespace}")
        return result
    except ApiException as e:
        if e.status == 409:
            logger.debug(f"PVC {name} already exists, skipping")
            return v1.read_namespaced_persistent_volume_claim(name, namespace)
        raise


def wait_for_pvcs_bound(namespace: str, pvc_names: list[str], timeout: int = 600):
    """
    Block until all named PVCs reach Bound status.
    Replaces the wait_for_ready_pvc() bash function.

    timeout: seconds before raising TimeoutError (default 10 min).
    """
    v1 = client.CoreV1Api()
    deadline = time.time() + timeout
    pending = set(pvc_names)

    logger.info(f"Waiting for {len(pending)} PVCs to become Bound...")
    while pending and time.time() < deadline:
        for name in list(pending):
            try:
                pvc = v1.read_namespaced_persistent_volume_claim(name, namespace)
                if pvc.status.phase == "Bound":
                    logger.info(f"PVC {name} is Bound")
                    pending.remove(name)
            except ApiException:
                pass
        if pending:
            time.sleep(5)

    if pending:
        raise TimeoutError(
            f"PVCs did not reach Bound within {timeout}s: {pending}"
        )
    logger.info("All PVCs are Bound")


def delete_pvc(name: str, namespace: str):
    v1 = client.CoreV1Api()
    try:
        v1.delete_namespaced_persistent_volume_claim(name, namespace)
        logger.info(f"Deleted PVC {name}")
    except ApiException as e:
        if e.status != 404:
            raise


# ── Deployments (database pods) ───────────────────────────────────────────────

def create_deployment(
    name: str,
    namespace: str,
    image: str,
    pvc_name: str,
    node_name: str,
    mount_path: str,
    env_vars: dict,
    port: int,
    resources: dict,
    labels: Optional[dict] = None,
) -> client.V1Deployment:
    """
    Create a single-replica Deployment for a database pod.
    Replaces create_postgresql(), create_mysql(), create_mongodb(),
    create_sqlserver() in the original create_databases bash script.

    The caller is responsible for passing the correct image and env_vars
    for each database type.
    """
    apps_v1 = client.AppsV1Api()

    lbl = labels or {"app": name}
    lbl["sherlock.io/role"] = "database"

    env = [
        client.V1EnvVar(name=k, value=v)
        for k, v in env_vars.items()
        if not isinstance(v, dict)
    ]
    # Support secretKeyRef env vars: {"MSSQL_SA_PASSWORD": {"secret": "sqlserver", "key": "SA_PASSWORD"}}
    for k, v in env_vars.items():
        if isinstance(v, dict) and "secret" in v:
            env.append(client.V1EnvVar(
                name=k,
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name=v["secret"], key=v["key"]
                    )
                )
            ))

    container = client.V1Container(
        name=name,
        image=image,
        image_pull_policy="IfNotPresent",
        ports=[client.V1ContainerPort(container_port=port)],
        env=env,
        volume_mounts=[
            client.V1VolumeMount(
                name="data", mount_path=mount_path
            )
        ],
        resources=client.V1ResourceRequirements(
            requests={
                "cpu":    resources.get("requests", {}).get("cpu", "1"),
                "memory": resources.get("requests", {}).get("memory", "2Gi"),
            },
            limits={
                "cpu":    resources.get("limits", {}).get("cpu", "2"),
                "memory": resources.get("limits", {}).get("memory", "4Gi"),
            },
        ),
    )

    pod_spec = client.V1PodSpec(
        node_selector={"kubernetes.io/hostname": node_name},
        restart_policy="Always",
        containers=[container],
        volumes=[
            client.V1Volume(
                name="data",
                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                    claim_name=pvc_name
                )
            )
        ],
    )

    deployment = client.V1Deployment(
        metadata=client.V1ObjectMeta(
            name=name, namespace=namespace, labels=lbl
        ),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(match_labels={"app": name}),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels={"app": name}),
                spec=pod_spec,
            ),
            strategy=client.V1DeploymentStrategy(
                type="RollingUpdate",
                rolling_update=client.V1RollingUpdateDeployment(
                    max_surge=1, max_unavailable=1
                )
            ),
        ),
    )

    try:
        result = apps_v1.create_namespaced_deployment(namespace, deployment)
        logger.info(f"Created deployment {name} on node {node_name}")
        return result
    except ApiException as e:
        if e.status == 409:
            logger.debug(f"Deployment {name} already exists")
            return apps_v1.read_namespaced_deployment(name, namespace)
        raise


def create_service(name: str, namespace: str, port: int, labels: Optional[dict] = None):
    """
    Create a ClusterIP Service for a database Deployment.
    Replaces create_service() in the original create_databases bash script.
    """
    v1 = client.CoreV1Api()
    lbl = labels or {"app": name}
    svc = client.V1Service(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace, labels=lbl),
        spec=client.V1ServiceSpec(
            type="ClusterIP",
            selector={"app": name},
            ports=[client.V1ServicePort(port=port)],
        ),
    )
    try:
        result = v1.create_namespaced_service(namespace, svc)
        logger.info(f"Created service {name} on port {port}")
        return result
    except ApiException as e:
        if e.status == 409:
            logger.debug(f"Service {name} already exists")
            return v1.read_namespaced_service(name, namespace)
        raise


def get_service_cluster_ip(name: str, namespace: str) -> str:
    """
    Return the ClusterIP of a service.
    Replaces: kubectl get svc -n ${NAMESPACE} ${name} -o jsonpath='{.spec.clusterIP}'
    """
    v1 = client.CoreV1Api()
    svc = v1.read_namespaced_service(name, namespace)
    return svc.spec.cluster_ip


def wait_for_deployment_ready(name: str, namespace: str, timeout: int = 300):
    """
    Wait until a Deployment has at least one ready replica.
    Replaces: kubectl wait --for=condition=Ready pod -l app=<name> --timeout=120s
    """
    apps_v1 = client.AppsV1Api()
    deadline = time.time() + timeout
    while time.time() < deadline:
        dep = apps_v1.read_namespaced_deployment(name, namespace)
        if dep.status.ready_replicas and dep.status.ready_replicas >= 1:
            logger.info(f"Deployment {name} is ready")
            return
        time.sleep(5)
    raise TimeoutError(f"Deployment {name} not ready within {timeout}s")


def delete_deployment(name: str, namespace: str):
    apps_v1 = client.AppsV1Api()
    try:
        apps_v1.delete_namespaced_deployment(name, namespace)
        logger.info(f"Deleted deployment {name}")
    except ApiException as e:
        if e.status != 404:
            raise


# ── Jobs ─────────────────────────────────────────────────────────────────────

def create_job(
    name: str,
    namespace: str,
    image: str,
    command: list[str],
    node_name: str,
    labels: dict,
    resources: dict,
    pvc_mounts: Optional[list[dict]] = None,
    host_network: bool = False,
    env_vars: Optional[dict] = None,
    ttl_seconds: int = 3600,
) -> client.V1Job:
    """
    Create a k8s Job.
    Replaces start_the_workload_job() and run_stats() in run_database_workload-parallel,
    and run_fio_job_fs() / run_fio_job_raw() in run_fio_job.

    pvc_mounts: list of {"pvc_name": str, "mount_path": str, "read_only": bool}
    ttl_seconds: auto-clean up the Job object after completion (default 1h)
    host_network: True only for stat collector pods (original used hostNetwork: true)
    """
    batch_v1 = client.BatchV1Api()

    volume_mounts = []
    volumes = []
    for i, m in enumerate(pvc_mounts or []):
        vol_name = f"vol-{i}"
        volume_mounts.append(client.V1VolumeMount(
            name=vol_name,
            mount_path=m["mount_path"],
            read_only=m.get("read_only", False),
        ))
        volumes.append(client.V1Volume(
            name=vol_name,
            persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                claim_name=m["pvc_name"],
                read_only=m.get("read_only", False),
            )
        ))

    env = [
        client.V1EnvVar(name=k, value=str(v))
        for k, v in (env_vars or {}).items()
    ]

    container = client.V1Container(
        name="benchmark",
        image=image,
        image_pull_policy="Always",
        command=command,
        volume_mounts=volume_mounts,
        env=env,
        resources=client.V1ResourceRequirements(
            requests={
                "cpu":    resources.get("requests", {}).get("cpu", "1"),
                "memory": resources.get("requests", {}).get("memory", "1Gi"),
            },
            limits={
                "cpu":    resources.get("limits", {}).get("cpu", "4"),
                "memory": resources.get("limits", {}).get("memory", "2Gi"),
            },
        ),
    )

    pod_spec = client.V1PodSpec(
        restart_policy="Never",
        node_selector={"kubernetes.io/hostname": node_name},
        containers=[container],
        volumes=volumes,
        host_network=host_network,
    )

    job = client.V1Job(
        metadata=client.V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels=labels,
        ),
        spec=client.V1JobSpec(
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels),
                spec=pod_spec,
            ),
            backoff_limit=0,             # no retries at Job level; operator handles retries
            ttl_seconds_after_finished=ttl_seconds,
        ),
    )

    try:
        result = batch_v1.create_namespaced_job(namespace, job)
        logger.info(f"Created job {name} on node {node_name}")
        return result
    except ApiException as e:
        if e.status == 409:
            logger.warning(f"Job {name} already exists")
            return batch_v1.read_namespaced_job(name, namespace)
        raise


def get_job_status(name: str, namespace: str) -> dict:
    """
    Return a dict with keys: succeeded, failed, active.
    Replaces: kubectl wait --for=condition=complete <job> --timeout=4000s
    The operator watches for these events rather than blocking.
    """
    batch_v1 = client.BatchV1Api()
    job = batch_v1.read_namespaced_job(name, namespace)
    return {
        "succeeded": job.status.succeeded or 0,
        "failed":    job.status.failed or 0,
        "active":    job.status.active or 0,
    }


def get_job_pod_logs(job_name: str, namespace: str) -> str:
    """
    Retrieve stdout logs from the pod that ran a Job.
    Replaces: kubectl logs <pod_name> > <run_dir>/<pod_name>.log
    """
    v1 = client.CoreV1Api()
    pods = v1.list_namespaced_pod(
        namespace,
        label_selector=f"job-name={job_name}"
    )
    if not pods.items:
        logger.warning(f"No pods found for job {job_name}")
        return ""
    pod_name = pods.items[0].metadata.name
    try:
        return v1.read_namespaced_pod_log(pod_name, namespace)
    except ApiException as e:
        logger.warning(f"Could not get logs for pod {pod_name}: {e}")
        return ""


def delete_job(name: str, namespace: str):
    batch_v1 = client.BatchV1Api()
    try:
        batch_v1.delete_namespaced_job(
            name, namespace,
            body=client.V1DeleteOptions(propagation_policy="Background")
        )
        logger.info(f"Deleted job {name}")
    except ApiException as e:
        if e.status != 404:
            raise


# ── Nodes ────────────────────────────────────────────────────────────────────

def get_worker_nodes(
    node_selector: Optional[dict] = None,
    count: Optional[int] = None,
) -> list[str]:
    """
    Return a list of worker node hostnames.
    Replaces reading from WORKERS_LIST_FILE in the original scripts.

    node_selector: optional label filter (e.g. {"node-role": "worker"})
    count: if set, return at most this many nodes
    """
    v1 = client.CoreV1Api()

    label_selector = None
    if node_selector:
        label_selector = ",".join(f"{k}={v}" for k, v in node_selector.items())

    nodes = v1.list_node(label_selector=label_selector)

    hostnames = []
    for node in nodes.items:
        # Skip master/control-plane nodes
        labels = node.metadata.labels or {}
        if (
            "node-role.kubernetes.io/master" in labels
            or "node-role.kubernetes.io/control-plane" in labels
        ):
            continue
        # Skip nodes that are not schedulable
        if node.spec.unschedulable:
            continue
        hostname = labels.get("kubernetes.io/hostname", node.metadata.name)
        hostnames.append(hostname)

    if count:
        hostnames = hostnames[:count]

    logger.info(f"Found {len(hostnames)} worker nodes: {hostnames}")
    return hostnames


def get_node_tolerations(node_name: str) -> list[dict]:
    """
    Return taints on a node as toleration dicts.
    Replaces find_taints() in the original bash scripts.
    """
    v1 = client.CoreV1Api()
    node = v1.read_node(node_name)
    tolerations = []
    for taint in (node.spec.taints or []):
        t = {"key": taint.key, "effect": taint.effect, "operator": "Equal"}
        if taint.value:
            t["value"] = taint.value
        else:
            t["operator"] = "Exists"
        tolerations.append(t)
    return tolerations


# ── ConfigMaps ────────────────────────────────────────────────────────────────

def create_configmap(
    name: str,
    namespace: str,
    data: dict,
    labels: Optional[dict] = None,
) -> client.V1ConfigMap:
    """
    Create a ConfigMap. Used for:
    - MySQL custom config (innodb_buffer_pool_size, etc.)
    - MongoDB init script (create user / db)
    - Result summaries written by the operator after each run
    """
    v1 = client.CoreV1Api()
    cm = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels=labels or {},
        ),
        data=data,
    )
    try:
        result = v1.create_namespaced_config_map(namespace, cm)
        logger.info(f"Created ConfigMap {name}")
        return result
    except ApiException as e:
        if e.status == 409:
            result = v1.replace_namespaced_config_map(name, namespace, cm)
            logger.info(f"Replaced ConfigMap {name}")
            return result
        raise


# ── Secrets ───────────────────────────────────────────────────────────────────

def create_secret(
    name: str,
    namespace: str,
    data: dict,
) -> client.V1Secret:
    """
    Create an Opaque Secret.
    Used for MSSQL SA_PASSWORD and S3 credentials.
    data values should be plain strings (base64 encoding is handled automatically).
    """
    import base64
    v1 = client.CoreV1Api()
    encoded = {k: base64.b64encode(v.encode()).decode() for k, v in data.items()}
    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        type="Opaque",
        data=encoded,
    )
    try:
        return v1.create_namespaced_secret(namespace, secret)
    except ApiException as e:
        if e.status == 409:
            logger.debug(f"Secret {name} already exists")
            return v1.read_namespaced_secret(name, namespace)
        raise
