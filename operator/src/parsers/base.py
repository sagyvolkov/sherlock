"""
parsers/base.py — Base stat output parser.

Parses the raw output from the stats container (iostat, vmstat, ip)
into structured dicts. This replaces the awk/grep/bc chains in the
original print_results and calculate_local_device_utilization bash functions.

All benchmark-specific parsers (pgbench.py, hammerdb.py, etc.) call
parse_storage_stats() from this module for the OS-level storage metrics.
They can override it if their storage plugin produces different output.

Phase 1: parse from iostat/vmstat text output (current)
Phase 2: query from Prometheus instead (future)
"""

import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ── iostat parser ─────────────────────────────────────────────────────────────

def parse_iostat(raw: str, device_filter: Optional[str] = None) -> dict:
    """
    Parse iostat -x output into per-device metrics, then average across
    all intervals. Returns a dict of aggregated storage metrics.

    iostat -x produces repeated blocks like:
      Device  r/s   w/s  rkB/s  wkB/s  r_await  w_await  %util
      nvme0n1 1200  300  48000  12000  0.5      1.2      45.0

    device_filter: if set, only include devices matching this substring
      (e.g. "nvme" to skip loop devices and sda).

    Replaces:
      grep ${device} ${file} | awk '{print $2","$8","$23}'
      and the averaging loop in calculate_local_device_utilization()
    """
    # Matches iostat -x extended output lines:
    # Device r/s w/s rkB/s wkB/s rrqm/s wrqm/s %rrqm %wrqm r_await w_await aqu-sz rareq-sz wareq-sz svctm %util
    IOSTAT_PATTERN = re.compile(
        r'^(?P<device>\S+)\s+'
        r'(?P<r_s>[\d.]+)\s+'    # r/s
        r'(?P<w_s>[\d.]+)\s+'    # w/s
        r'(?P<rkb_s>[\d.]+)\s+'  # rkB/s
        r'(?P<wkb_s>[\d.]+)\s+'  # wkB/s
        r'[\d.]+\s+[\d.]+\s+'    # rrqm/s wrqm/s
        r'[\d.]+\s+[\d.]+\s+'    # %rrqm %wrqm
        r'(?P<r_await>[\d.]+)\s+'  # r_await (ms)
        r'(?P<w_await>[\d.]+)\s+'  # w_await (ms)
        r'[\d.]+\s+'               # aqu-sz
        r'[\d.]+\s+[\d.]+\s+'     # rareq-sz wareq-sz
        r'(?:[\d.]+\s+)?'          # svctm (optional, removed in newer kernels)
        r'(?P<util>[\d.]+)',        # %util
        re.MULTILINE
    )

    device_stats: dict[str, list] = {}

    for match in IOSTAT_PATTERN.finditer(raw):
        device = match.group("device")
        if device_filter and device_filter not in device:
            continue
        # Skip loop devices, dm- devices (device mapper), and common non-data devices
        if device.startswith(("loop", "dm-", "sr")):
            continue

        stat = {
            "r_iops":   float(match.group("r_s")),
            "w_iops":   float(match.group("w_s")),
            "r_bw_kbs": float(match.group("rkb_s")),
            "w_bw_kbs": float(match.group("wkb_s")),
            "r_await":  float(match.group("r_await")),
            "w_await":  float(match.group("w_await")),
            "util":     float(match.group("util")),
        }
        device_stats.setdefault(device, []).append(stat)

    if not device_stats:
        logger.warning("No iostat device data found in output")
        return {}

    # Aggregate: sum across devices, average across intervals
    totals: dict[str, float] = {
        "r_iops": 0, "w_iops": 0,
        "r_bw_kbs": 0, "w_bw_kbs": 0,
        "r_await": 0, "w_await": 0,
        "util": 0,
    }
    n_intervals = 0

    for device, intervals in device_stats.items():
        n_intervals = max(n_intervals, len(intervals))
        for interval in intervals:
            for key in totals:
                totals[key] += interval[key]

    if n_intervals == 0:
        return {}

    # Average over all devices × intervals
    n = n_intervals * len(device_stats)
    return {
        "avgReadIops":    round(totals["r_iops"]   / n, 2),
        "avgWriteIops":   round(totals["w_iops"]   / n, 2),
        "avgReadBwMBs":   round(totals["r_bw_kbs"] / n / 1024, 2),  # kB/s → MB/s
        "avgWriteBwMBs":  round(totals["w_bw_kbs"] / n / 1024, 2),
        "avgLatencyMs":   round(
            (totals["r_await"] + totals["w_await"]) / (2 * n), 2
        ),
        "avgUtilPct":     round(totals["util"] / n, 2),
        # p95/p99 require per-interval data sorted — we'll compute these
        # properly when the Prometheus integration lands. For now, approximate
        # as the max observed value across intervals.
        "p95LatencyMs":   _approx_percentile(device_stats, "r_await", 0.95),
        "p99LatencyMs":   _approx_percentile(device_stats, "r_await", 0.99),
    }


def _approx_percentile(
    device_stats: dict,
    field: str,
    pct: float,
) -> float:
    """
    Approximate a latency percentile from per-interval samples.
    Not statistically rigorous but better than nothing for the Phase 1 parser.
    Phase 2 will get proper percentiles from Prometheus histograms.
    """
    all_values = []
    for intervals in device_stats.values():
        for interval in intervals:
            if field in interval:
                all_values.append(interval[field])
    if not all_values:
        return 0.0
    all_values.sort()
    idx = int(len(all_values) * pct)
    return round(all_values[min(idx, len(all_values) - 1)], 2)


# ── vmstat parser ─────────────────────────────────────────────────────────────

def parse_vmstat(raw: str) -> dict:
    """
    Parse vmstat output into basic system utilisation metrics.
    vmstat output:
      procs  memory         swap  io    system  cpu
      r  b   swpd  free  buff  cache  si  so  bi  bo  in  cs  us  sy  id  wa  st
      2  0   0  1234567  12345  678901  0  0   0  1234  5678  9012  15  5  75  5  0

    We focus on the wa (iowait) column as a cross-check on the iostat data.
    """
    VMSTAT_PATTERN = re.compile(
        r'^\s*\d+\s+\d+\s+'         # r b
        r'[\d]+\s+[\d]+\s+'          # swpd free
        r'[\d]+\s+[\d]+\s+'          # buff cache
        r'[\d]+\s+[\d]+\s+'          # si so
        r'[\d]+\s+[\d]+\s+'          # bi bo
        r'[\d]+\s+[\d]+\s+'          # in cs
        r'[\d]+\s+[\d]+\s+[\d]+\s+'  # us sy id
        r'(?P<wa>[\d]+)',             # wa (iowait %)
        re.MULTILINE
    )

    wa_values = [
        float(m.group("wa"))
        for m in VMSTAT_PATTERN.finditer(raw)
    ]

    if not wa_values:
        return {}

    return {
        "avgIoWaitPct": round(sum(wa_values) / len(wa_values), 2),
        "maxIoWaitPct": round(max(wa_values), 2),
    }


# ── Combined storage stats ────────────────────────────────────────────────────

def parse_storage_stats(iostat_output: str, vmstat_output: str) -> dict:
    """
    Parse both iostat and vmstat outputs and merge into a single storage
    metrics dict matching the CRD status.runMatrix[].result schema.

    Called by all benchmark-specific parsers to populate the shared
    storage metrics fields.
    """
    iostat_metrics = parse_iostat(iostat_output)
    vmstat_metrics = parse_vmstat(vmstat_output)
    return {**iostat_metrics, **vmstat_metrics}


# ── SUMMARY line parser (v1 compatibility) ────────────────────────────────────

def parse_summary_lines(log_content: str) -> list[dict]:
    """
    Parse SUMMARY lines written by calculate_local_device_utilization()
    in the original run_database_workload-parallel script.

    Format:
      SUMMARY: <node>, averages for local device <dev> read/s: <r>, write/s: <w>, utilization%: <u>

    Used when reading v1-format stat logs for backward compatibility.
    """
    SUMMARY_PATTERN = re.compile(
        r'SUMMARY:\s+(?P<node>\S+),\s+averages for local device\s+(?P<device>\S+)\s+'
        r'read/s:\s+(?P<read_s>[\d.]+),\s+write/s:\s+(?P<write_s>[\d.]+),\s+'
        r'utilization%:\s+(?P<util>[\d.]+)'
    )

    results = []
    for m in SUMMARY_PATTERN.finditer(log_content):
        results.append({
            "node":      m.group("node"),
            "device":    m.group("device"),
            "readIops":  float(m.group("read_s")),
            "writeIops": float(m.group("write_s")),
            "utilPct":   float(m.group("util")),
        })
    return results
