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

# Default stat collection parameters
DEFAULT_STATS_INTERVAL = 10   # seconds between collections
DEFAULT_STATS_COUNT = 60      # number of intervals (interval × count = total duration)


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
    """
    jobs = {}
    for node_name in worker_nodes:
        job_name = f"stats-{node_type}-{run_name}-{node_name}"
        # Truncate to k8s name limit (63 chars)
        if len(job_name) > 63:
            job_name = job_name[:63]

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
                # hostNetwork was used in v1 for network interface stats.
                # In v2 we default to False (standard k8s) and rely on
                # /sys/class/net inside the container which is available
                # without hostNetwork on most CNIs.
                # Set to True via OCP Kustomize overlay if needed.
                host_network=False,
                ttl_seconds=7200,  # keep job for 2h after completion for log collection
            )
            jobs[job_name] = node_name
            logger.info(f"Launched stat collector on {node_name}: {job_name}")
        except Exception as e:
            logger.warning(f"Failed to launch stat collector on {node_name}: {e}")

    return jobs


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
