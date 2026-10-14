"""
plugins/storage/generic.py — Generic storage stat collector.

Launches stat collector Jobs on worker nodes to run iostat, vmstat, and ip.
This is the 'generic' storage plugin — it works with any storage backend
by collecting OS-level block device stats from the nodes where DB/fio pods run.

Replaces run_stats() and stats_collect() in run_database_workload-parallel
and the equivalent in run_fio_job.

The stat container image (quay.io/sagyvolkov/stats-container:sherlock0.7)
runs: ./run_all_scripts <interval> <count> <node_type>
which internally runs iostat, vmstat, and ip/ifconfig on a timer.

Phase 1 (current): raw logs collected, parsed by Python parsers/base.py
Phase 2 (future):  Prometheus node-exporter integration as an alternative
"""

import logging
from typing import Optional

from src.utils import k8s

logger = logging.getLogger(__name__)

# Stats container — same image as sherlock v1 for now
STATS_IMAGE = "quay.io/sagyvolkov/stats-container:sherlock0.7"

# Service account for collector pods. hostNetwork needs an SCC that allows
# it (hostnetwork-v2 on OpenShift); see rbac/operator-rbac.yaml.
STATS_SERVICE_ACCOUNT = "sherlock-stats"

# Default stat collection parameters
DEFAULT_STATS_INTERVAL = 10   # seconds between collections
DEFAULT_STATS_COUNT = 60      # number of intervals (interval × count = total duration)

# Extra collection time beyond the benchmark runtime. Collectors start
# before the benchmark pods, which can take a while to schedule and start;
# metrics are totals, so idle time at either end does not skew them.
DEFAULT_STATS_MARGIN_SECS = 30


def stats_settings(artifacts_spec: dict) -> tuple[int, int]:
    """(sample interval, margin) in seconds from artifacts.statsIntervalSeconds
    and artifacts.statsMarginSeconds."""
    spec = artifacts_spec or {}
    return (int(spec.get("statsIntervalSeconds") or DEFAULT_STATS_INTERVAL),
            int(spec.get("statsMarginSeconds", DEFAULT_STATS_MARGIN_SECS)))


def stats_count(runtime_secs: int, interval: int = DEFAULT_STATS_INTERVAL,
                margin_secs: int = DEFAULT_STATS_MARGIN_SECS) -> int:
    """Number of samples covering the benchmark runtime plus the margin."""
    return -(-(int(runtime_secs) + int(margin_secs)) // int(interval))


def launch_stat_collectors(
    suite_name: str,
    run_name: str,
    namespace: str,
    worker_nodes: list[str],
    stats_interval: int = DEFAULT_STATS_INTERVAL,
    stats_count: int = DEFAULT_STATS_COUNT,
    network_interfaces: Optional[dict] = None,  # {hostname: "eth0 eth1"}
    sds_devices: str = "",                      # "nvme0n1 nvme1n1"
    node_type: str = "worker",
    owner: Optional[dict] = None,
    name_key: str = "",
) -> dict[str, str]:
    """
    Launch one stat collector Job per worker node.
    Returns {job_name: node_name} for later log collection.

    Replaces stats_collect() in run_database_workload-parallel.

    network_interfaces: maps hostname to space-separated interface names.
      In the original scripts this was NODE_NETWORK_MAP in sherlock.config.
      In v2 this is optional — if not provided, ip/ifconfig runs on all interfaces.

    sds_devices: space-separated block device names to track (e.g. "nvme0n1 nvme1n1").
      Passed into /tmp/sds_devices inside the container.
      In the original scripts this was SDS_DEVICES in sherlock.config.

    node_type: "worker" or "sds" — passed to the container's run_all_scripts
      to distinguish which stats to collect.

    owner: suite CR that owns the Jobs (garbage-collected with the suite).

    name_key: makes Job names unique per suite instance and attempt
      (suite UID / run name / retry count). Defaults to run_name.
    """
    jobs = {}
    for i, node_name in enumerate(worker_nodes):
        # Previously f"stats-{node_type}-{run_name}-{node_name}"[:63], which
        # cut off the node name for long run names, so every node's collector
        # got the same name and only the first one was created.
        job_name = k8s.job_name(
            f"stats-{node_type}-{i}",
            f"{name_key or run_name}/stats/{node_type}/{node_name}",
        )

        interfaces = ""
        if network_interfaces:
            interfaces = network_interfaces.get(node_name, "")

        # The container command writes network interfaces and device names to
        # /tmp/ then calls run_all_scripts — same as the original bash scripts:
        #   echo "${network_interfaces}" > /tmp/network_interfaces
        #   echo "${SDS_DEVICES}" > /tmp/sds_devices
        #   ./run_all_scripts ${STATS_INTERVAL} ${STATS_COUNT} ${node_type}
        command = [
            "bash", "-c",
            (
                f'echo "{interfaces}" > /tmp/network_interfaces; '
                f'echo "{sds_devices}" > /tmp/sds_devices; '
                f'./run_all_scripts {stats_interval} {stats_count} {node_type}'
            )
        ]

        labels = {
            "sherlock.io/suite":     suite_name,
            "sherlock.io/run":       run_name,
            "sherlock.io/role":      "stat-collector",
            "sherlock.io/node-type": node_type,
        }

        try:
            k8s.create_job(
                name=job_name,
                namespace=namespace,
                image=STATS_IMAGE,
                command=command,
                node_name=node_name,
                labels=labels,
                resources={
                    "requests": {"cpu": "0.1", "memory": "64Mi"},
                    "limits":   {"cpu": "0.1", "memory": "64Mi"},
                },
                # The node's network namespace: storage traffic (NVMe/TCP,
                # iSCSI, Ceph) is sent by the host kernel on the node's NICs,
                # which a pod network namespace cannot see. iostat reads
                # /proc/diskstats, which is host-wide either way.
                host_network=True,
                service_account=STATS_SERVICE_ACCOUNT,
                ttl_seconds=7200,  # keep job for 2h after completion for log collection
                owner=owner,
            )
            jobs[job_name] = node_name
            logger.info(f"Launched stat collector on {node_name}: {job_name}")
        except Exception as e:
            logger.warning(f"Failed to launch stat collector on {node_name}: {e}")

    return jobs


def wait_for_collectors_running(stat_jobs: dict[str, str], namespace: str,
                                timeout: int = 120) -> None:
    """
    Wait until every collector pod is running, so the benchmark starts
    inside the collection window and its first seconds are counted.
    Gives up after timeout (the run still goes ahead, with a warning).
    """
    import time
    pending = set(stat_jobs)
    deadline = time.time() + timeout
    while pending and time.time() < deadline:
        pending = {j for j in pending
                   if k8s.get_job_pod_phase(j, namespace) not in ("Running", "Succeeded")}
        if pending:
            time.sleep(2)
    if pending:
        logger.warning(f"Stat collectors not running after {timeout}s: {sorted(pending)}")


def collect_stat_logs(
    stat_jobs: dict[str, str],
    namespace: str,
) -> dict[str, str]:
    """
    Collect logs from all stat collector Jobs.
    Returns {node_name: raw_log_content}.

    Replaces the log collection loop in run_database_workload-parallel:
      for i in "${!jobs_pods[@]}"; do
        kubectl -n ${NAMESPACE} logs ${jobs_pods[$i]} > ${RUN_NAME}/${jobs_pods[$i]}.log
    """
    logs = {}
    for job_name, node_name in stat_jobs.items():
        raw = k8s.get_job_pod_logs(job_name, namespace)
        logs[node_name] = raw
        logger.debug(f"Collected {len(raw)} bytes of stats from {node_name}")
    return logs


def cleanup_stat_jobs(stat_jobs: dict[str, str], namespace: str):
    """Delete all stat collector Jobs (TTL handles this automatically, but
    this allows early cleanup if needed)."""
    for job_name in stat_jobs:
        k8s.delete_job(job_name, namespace)
