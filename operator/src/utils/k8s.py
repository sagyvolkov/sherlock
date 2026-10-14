"""
k8s.py — Kubernetes client helpers.

Thin wrappers around the kubernetes-client Python library.
All handlers use these instead of calling the k8s API directly,
which keeps the API surface small and makes testing easier.

Replaces the kubectl calls in the original bash scripts.
"""

import hashlib
import logging
import time
from typing import Optional

from kubernetes import client, config
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)

# ── Client initialisation ────────────────────────────────────────────────────

_config_loaded = False


def load_config():
    """
    Load k8s config — in-cluster when running as a pod, local kubeconfig
    when running for development. Only the first call does anything.
    """
    global _config_loaded
    if _config_loaded:
        return
    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster k8s config")
    except config.ConfigException:
        config.load_kube_config()
        logger.info("Loaded local kubeconfig")
    _config_loaded = True


# ── Namespace ────────────────────────────────────────────────────────────────



def patch_suite_status(group: str, version: str, plural: str,
                       name: str, namespace: str, status_patch: dict,
                       resource_version: Optional[str] = None) -> None:
    """
    Patch the status subresource of a namespaced custom resource.

    resource_version: if given, the patch only applies when the object is
    still at that version; otherwise the API server returns 409 Conflict
    (raised as ApiException). Used to claim a run exactly once when several
    Job events race.
    """
    load_config()
    from kubernetes import client
    api = client.CustomObjectsApi()
    body = {"status": status_patch}
    if resource_version:
        body["metadata"] = {"resourceVersion": resource_version}
    api.patch_namespaced_custom_object_status(
        group, version, namespace, plural, name, body,
    )

def get_custom_resource(group: str, version: str, plural: str, name: str, namespace: str) -> dict:
    """Fetch a single namespaced custom resource. Returns None if not found."""
    load_config()
    from kubernetes import client
    api = client.CustomObjectsApi()
    try:
        return api.get_namespaced_custom_object(group, version, namespace, plural, name)
    except client.exceptions.ApiException as e:
        if e.status == 404:
            return None
        raise

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
    annotations: Optional[dict] = None,
    access_mode: str = "ReadWriteOnce",
) -> client.V1PersistentVolumeClaim:
    """
    Create a PVC and return it.
    Replaces the create_pvc() bash function in create_databases and run_fio_job.

    volume_mode: "Filesystem" (default) or "Block" (for raw fio block tests).
    storage_class: empty string uses the cluster default StorageClass.
    """
    v1 = client.CoreV1Api()
    spec = client.V1PersistentVolumeClaimSpec(
        access_modes=[access_mode],
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
            annotations=annotations or None,
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


def list_pvcs(namespace: str) -> list:
    """All PVCs in the namespace."""
    return client.CoreV1Api().list_namespaced_persistent_volume_claim(namespace).items


def read_pvc(name: str, namespace: str):
    """Return the PVC, or None if it does not exist."""
    try:
        return client.CoreV1Api().read_namespaced_persistent_volume_claim(name, namespace)
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def patch_pvc_metadata(name: str, namespace: str,
                       labels: Optional[dict] = None,
                       annotations: Optional[dict] = None):
    """Merge labels/annotations into a PVC; a None value removes the key."""
    metadata = {}
    if labels is not None:
        metadata["labels"] = labels
    if annotations is not None:
        metadata["annotations"] = annotations
    client.CoreV1Api().patch_namespaced_persistent_volume_claim(
        name, namespace, {"metadata": metadata},
    )


def wait_for_pvcs_deleted(namespace: str, pvc_names: list[str], timeout: int = 600):
    """
    Block until none of the named PVCs exist any more. A PVC stays
    Terminating while a pod still mounts it (kubernetes.io/pvc-protection).
    """
    deadline = time.time() + timeout
    pending = set(pvc_names)
    while pending and time.time() < deadline:
        pending = {n for n in pending if read_pvc(n, namespace) is not None}
        if pending:
            time.sleep(5)
    if pending:
        raise TimeoutError(f"PVCs not deleted within {timeout}s: {sorted(pending)}")
    logger.info(f"PVCs deleted: {sorted(pvc_names)}")


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
    owner: Optional[dict] = None,
    config_mounts: Optional[list[dict]] = None,
    add_capabilities: Optional[list[str]] = None,
) -> client.V1Deployment:
    """
    Create a single-replica Deployment for a database pod.
    Replaces create_postgresql(), create_mysql(), create_mongodb(),
    create_sqlserver() in the original create_databases bash script.

    The caller is responsible for passing the correct image and env_vars
    for each database type.

    owner: suite CR that owns the Deployment, which is then garbage-collected
    with the suite. Its PVC is not owned and outlives both.
    config_mounts: [{"configmap": name, "mount_path": dir}] mounted read-only.
    add_capabilities: Linux capabilities the container needs (e.g. MSSQL's
      sqlservr binary carries cap_net_bind_service and cannot be executed
      without NET_BIND_SERVICE under OpenShift's restricted-v2 SCC).

    The pod is Ready only once the database accepts TCP connections on
    `port`. Database images initialise a new data directory with a
    temporary server that does not listen on TCP (MySQL takes 20-60s), so
    without the probe the first prepare Job could start too early.
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
        ] + [
            client.V1VolumeMount(name=f"config-{i}", mount_path=m["mount_path"],
                                 read_only=True)
            for i, m in enumerate(config_mounts or [])
        ],
        security_context=client.V1SecurityContext(
            capabilities=client.V1Capabilities(add=add_capabilities),
        ) if add_capabilities else None,
        readiness_probe=client.V1Probe(
            tcp_socket=client.V1TCPSocketAction(port=port),
            initial_delay_seconds=5,
            period_seconds=5,
            failure_threshold=3,
        ),
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
        ] + [
            client.V1Volume(
                name=f"config-{i}",
                config_map=client.V1ConfigMapVolumeSource(name=m["configmap"]),
            )
            for i, m in enumerate(config_mounts or [])
        ],
    )

    deployment = client.V1Deployment(
        metadata=client.V1ObjectMeta(
            name=name, namespace=namespace, labels=lbl,
            owner_references=[owner_reference(owner)] if owner else None,
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


def create_service(name: str, namespace: str, port: int, labels: Optional[dict] = None,
                   owner: Optional[dict] = None):
    """
    Create a ClusterIP Service for a database Deployment.
    Replaces create_service() in the original create_databases bash script.

    owner: suite CR that owns the Service (garbage-collected with it).
    """
    v1 = client.CoreV1Api()
    lbl = labels or {"app": name}
    svc = client.V1Service(
        metadata=client.V1ObjectMeta(
            name=name, namespace=namespace, labels=lbl,
            owner_references=[owner_reference(owner)] if owner else None,
        ),
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


def list_deployments(namespace: str) -> list:
    """All Deployments in the namespace."""
    return client.AppsV1Api().list_namespaced_deployment(namespace).items


def read_deployment(name: str, namespace: str):
    """Return the Deployment, or None if it does not exist."""
    try:
        return client.AppsV1Api().read_namespaced_deployment(name, namespace)
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def delete_deployment(name: str, namespace: str, wait: bool = False, timeout: int = 300):
    """
    Delete a Deployment. wait=True uses foreground deletion and blocks until
    the Deployment, and so its pods, are gone, which releases their PVCs.
    """
    apps_v1 = client.AppsV1Api()
    try:
        apps_v1.delete_namespaced_deployment(
            name, namespace,
            body=client.V1DeleteOptions(
                propagation_policy="Foreground" if wait else "Background"
            ),
        )
        logger.info(f"Deleted deployment {name}")
    except ApiException as e:
        if e.status != 404:
            raise
    if not wait:
        return
    deadline = time.time() + timeout
    while read_deployment(name, namespace) is not None:
        if time.time() > deadline:
            raise TimeoutError(f"Deployment {name} not deleted within {timeout}s")
        time.sleep(3)


def list_services(namespace: str) -> list:
    """All Services in the namespace."""
    return client.CoreV1Api().list_namespaced_service(namespace).items


def delete_service(name: str, namespace: str):
    try:
        client.CoreV1Api().delete_namespaced_service(name, namespace)
        logger.info(f"Deleted service {name}")
    except ApiException as e:
        if e.status != 404:
            raise


# ── Jobs ─────────────────────────────────────────────────────────────────────

def job_name(prefix: str, key: str, max_len: int = 63) -> str:
    """
    Build a Job name that is unique per key and always fits in max_len.

    The readable prefix is truncated if needed; uniqueness comes from a hash
    of key, which callers build from the suite UID, run name and attempt.
    Two suites with the same name, or two runs whose names only differ
    past the truncation point, therefore never share a Job name.
    """
    digest = hashlib.sha1(key.encode()).hexdigest()[:10]
    head = prefix[: max_len - len(digest) - 1].rstrip("-.")
    return f"{head}-{digest}"


def owner_reference(owner: dict) -> client.V1OwnerReference:
    """
    Owner reference pointing at a suite CR (kopf `body` or a fetched object).

    Jobs owned by a suite are garbage-collected when the suite is deleted,
    and their ownerReferences UID tells a stale Job (from an earlier suite
    with the same name) apart from a current one.

    blockOwnerDeletion is False so no extra RBAC on <plural>/finalizers is
    needed and deleting a suite is never held up by its Jobs.
    """
    meta = owner["metadata"]
    return client.V1OwnerReference(
        api_version=owner["apiVersion"],
        kind=owner["kind"],
        name=meta["name"],
        uid=meta["uid"],
        controller=True,
        block_owner_deletion=False,
    )


def parse_duration_secs(value: Optional[str], default: int) -> int:
    """Parse a CRD duration like '30m' or '1h' (pattern ^[0-9]+(m|h)$)."""
    if not value:
        return default
    value = str(value).strip()
    try:
        if value.endswith("h"):
            return int(value[:-1]) * 3600
        if value.endswith("m"):
            return int(value[:-1]) * 60
        return int(value)
    except ValueError:
        logger.warning(f"Unparseable duration {value!r}, using {default}s")
        return default


# CPU limit of a Job when its resources do not set one
DEFAULT_JOB_CPU_LIMIT = "4"


def create_job(
    name: str,
    namespace: str,
    image: str,
    command: list[str],
    node_name: Optional[str],
    labels: dict,
    resources: dict,
    pvc_mounts: Optional[list[dict]] = None,
    host_network: bool = False,
    env_vars: Optional[dict] = None,
    ttl_seconds: int = 3600,
    owner: Optional[dict] = None,
    active_deadline_seconds: Optional[int] = None,
    configmap_mount: Optional[dict] = None,
    block_devices: Optional[list[dict]] = None,
    service_account: Optional[str] = None,
    secret_env: Optional[dict] = None,
) -> client.V1Job:
    """
    Create a k8s Job.
    Replaces start_the_workload_job() and run_stats() in run_database_workload-parallel,
    and run_fio_job_fs() / run_fio_job_raw() in run_fio_job.

    node_name: node to run on; None lets the scheduler pick
    pvc_mounts: list of {"pvc_name": str, "mount_path": str, "read_only": bool}
    configmap_mount: {"mount_path": str, "items": [(configmap, key, file), ...]}
      projects keys of one or more ConfigMaps into one read-only directory
    block_devices: list of {"pvc_name": str, "device_path": str} for
      volumeMode=Block PVCs, exposed as raw devices (volumeDevices)
    service_account: pod service account (default: the namespace default)
    secret_env: {env var: (secret name, key)} set from Secrets (secretKeyRef),
      for credentials that must not appear in the Job spec
    ttl_seconds: auto-clean up the Job object after completion (default 1h)
    host_network: True only for stat collector pods (original used hostNetwork: true)
    owner: suite CR (kopf body) that owns the Job; sets ownerReferences
    active_deadline_seconds: Job is marked Failed (DeadlineExceeded) after this
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

    if configmap_mount:
        by_configmap: dict = {}
        for cm_name, key, path in configmap_mount["items"]:
            by_configmap.setdefault(cm_name, []).append(
                client.V1KeyToPath(key=key, path=path))
        volume_mounts.append(client.V1VolumeMount(
            name="configmaps", mount_path=configmap_mount["mount_path"], read_only=True,
        ))
        volumes.append(client.V1Volume(
            name="configmaps",
            projected=client.V1ProjectedVolumeSource(sources=[
                client.V1VolumeProjection(config_map=client.V1ConfigMapProjection(
                    name=cm_name, items=items))
                for cm_name, items in by_configmap.items()
            ]),
        ))

    volume_devices = []
    for i, d in enumerate(block_devices or []):
        vol_name = f"dev-{i}"
        volume_devices.append(client.V1VolumeDevice(
            name=vol_name, device_path=d["device_path"]))
        volumes.append(client.V1Volume(
            name=vol_name,
            persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                claim_name=d["pvc_name"]),
        ))

    env = [
        client.V1EnvVar(name=k, value=str(v))
        for k, v in (env_vars or {}).items()
    ] + [
        client.V1EnvVar(name=var, value_from=client.V1EnvVarSource(
            secret_key_ref=client.V1SecretKeySelector(name=secret, key=key)))
        for var, (secret, key) in (secret_env or {}).items()
    ]

    container = client.V1Container(
        name="benchmark",
        image=image,
        image_pull_policy="Always",
        command=command,
        volume_mounts=volume_mounts,
        volume_devices=volume_devices or None,
        env=env,
        resources=client.V1ResourceRequirements(
            requests={
                "cpu":    resources.get("requests", {}).get("cpu", "1"),
                "memory": resources.get("requests", {}).get("memory", "1Gi"),
            },
            limits={
                "cpu":    resources.get("limits", {}).get("cpu", DEFAULT_JOB_CPU_LIMIT),
                "memory": resources.get("limits", {}).get("memory", "2Gi"),
            },
        ),
    )

    pod_spec = client.V1PodSpec(
        restart_policy="Never",
        node_selector={"kubernetes.io/hostname": node_name} if node_name else None,
        containers=[container],
        volumes=volumes,
        host_network=host_network,
        service_account_name=service_account,
    )

    job = client.V1Job(
        metadata=client.V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels=labels,
            owner_references=[owner_reference(owner)] if owner else None,
        ),
        spec=client.V1JobSpec(
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels),
                spec=pod_spec,
            ),
            backoff_limit=0,             # no retries at Job level; operator handles retries
            ttl_seconds_after_finished=ttl_seconds,
            active_deadline_seconds=active_deadline_seconds,
        ),
    )

    try:
        result = batch_v1.create_namespaced_job(namespace, job)
        logger.info(f"Created job {name} on node {node_name or '(any)'}")
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


def get_job_state(name: str, namespace: str) -> str:
    """
    Return "Complete", "Failed", "Active" or "Missing" for a Job.

    Uses the terminal conditions rather than the pod counters, so a Job that
    hit activeDeadlineSeconds counts as Failed even while its pod is still
    being torn down. "Missing" means the Job is gone (deleted or TTL'd).
    """
    batch_v1 = client.BatchV1Api()
    try:
        job = batch_v1.read_namespaced_job(name, namespace)
    except ApiException as e:
        if e.status == 404:
            return "Missing"
        raise
    return job_state_from_conditions(
        [{"type": c.type, "status": c.status}
         for c in (job.status.conditions or [])]
    )


def job_state_from_conditions(conditions: list[dict]) -> str:
    """Map Job status.conditions (as dicts) to Complete / Failed / Active."""
    for c in conditions or []:
        if c.get("status") != "True":
            continue
        if c.get("type") == "Complete":
            return "Complete"
        if c.get("type") == "Failed":
            return "Failed"
    return "Active"


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
        # _preload_content=False: when the log is valid JSON (fio
        # --output-format=json) the client otherwise parses it and returns
        # the Python repr ({'fio version': ...}), which is not JSON.
        resp = v1.read_namespaced_pod_log(pod_name, namespace, _preload_content=False)
        return resp.data.decode("utf-8", errors="replace")
    except ApiException as e:
        logger.warning(f"Could not get logs for pod {pod_name}: {e}")
        return ""


def get_job_pod_phase(job_name: str, namespace: str) -> Optional[str]:
    """Phase of the Job's pod (Pending, Running, Succeeded, ...), or None."""
    pods = client.CoreV1Api().list_namespaced_pod(
        namespace, label_selector=f"job-name={job_name}").items
    return pods[0].status.phase if pods else None


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
    data: Optional[dict] = None,
    labels: Optional[dict] = None,
    binary_data: Optional[dict] = None,
    owner: Optional[dict] = None,
) -> client.V1ConfigMap:
    """
    Create a ConfigMap. Used for:
    - MySQL custom config (innodb_buffer_pool_size, etc.)
    - MongoDB init script (create user / db)
    - Artifact bundle chunks handed to the PVC writer Job

    binary_data: {key: bytes}; base64-encoded here.
    owner: suite CR that owns the ConfigMap (garbage-collected with it).
    """
    import base64
    v1 = client.CoreV1Api()
    cm = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels=labels or {},
            owner_references=[owner_reference(owner)] if owner else None,
        ),
        data=data,
        binary_data={k: base64.b64encode(v).decode() for k, v in binary_data.items()}
        if binary_data else None,
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


def delete_configmap(name: str, namespace: str):
    try:
        client.CoreV1Api().delete_namespaced_config_map(name, namespace)
    except ApiException as e:
        if e.status != 404:
            raise


# ── Secrets ───────────────────────────────────────────────────────────────────

def create_secret(
    name: str,
    namespace: str,
    data: dict,
    owner: Optional[dict] = None,
    labels: Optional[dict] = None,
) -> client.V1Secret:
    """
    Create (or replace) an Opaque Secret.
    Used for the per-suite database credentials.
    data values should be plain strings (base64 encoding is handled automatically).
    owner: suite CR that owns the Secret (garbage-collected with it).
    """
    import base64
    v1 = client.CoreV1Api()
    encoded = {k: base64.b64encode(v.encode()).decode() for k, v in data.items()}
    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name=name, namespace=namespace, labels=labels,
            owner_references=[owner_reference(owner)] if owner else None,
        ),
        type="Opaque",
        data=encoded,
    )
    try:
        return v1.create_namespaced_secret(namespace, secret)
    except ApiException as e:
        if e.status == 409:
            # Replace, so changed credentials take effect
            logger.debug(f"Secret {name} already exists, replacing")
            return v1.replace_namespaced_secret(name, namespace, secret)
        raise
