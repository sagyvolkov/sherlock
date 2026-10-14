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
#
# The stats container prefixes every line with its source (IOSTAT-, VMSTAT-,
# IFCONFIG.BEFORE-, ...) and runs `iostat -dtxmyz <interval> <count>`:
# timestamped samples, extended columns in MB, idle devices omitted. Column
# order differs between sysstat versions, so columns are located by header
# name. Totals are each sample's rate times the sample length, so they do
# not depend on how much of the collection window was idle.

# NVMe namespace heads (nvme0n1). Excludes multipath path devices
# (nvme0c0n1), which repeat the head's I/O, and partitions.
DEFAULT_DEVICE_PATTERN = r"^nvme\d+n\d+$"

_TIMESTAMP_RE = re.compile(
    r"^(\d{2}/\d{2}/\d{2,4})\s+(\d{1,2}:\d{2}:\d{2})(?:\s*([AP]M))?\s*$")


def _strip_prefix(raw: str, prefix: str) -> list[str]:
    """Lines of one source, prefix removed; unprefixed text is kept as is."""
    if prefix not in raw:
        return raw.splitlines()
    return [l[len(prefix):] for l in raw.splitlines() if l.startswith(prefix)]


def _parse_timestamp(date: str, clock: str, ampm: Optional[str]) -> Optional[float]:
    from datetime import datetime
    fmt = "%m/%d/%y" if len(date) == 8 else "%m/%d/%Y"
    try:
        if ampm:
            return datetime.strptime(f"{date} {clock} {ampm}", f"{fmt} %I:%M:%S %p").timestamp()
        return datetime.strptime(f"{date} {clock}", f"{fmt} %H:%M:%S").timestamp()
    except ValueError:
        return None


def parse_iostat_devices(raw: str, interval: float = 10.0) -> dict:
    """
    Per-device totals over all iostat samples:
      {device: {readMiB, writeMiB, readIos, writeIos, readAwaitMs,
                writeAwaitMs, maxUtilPct, samples}}
    readAwaitMs / writeAwaitMs are averages weighted by I/O count.
    interval is the sample length when it cannot be read from timestamps.
    """
    devices: dict = {}
    columns: Optional[dict] = None
    sample_secs = float(interval)
    prev_ts: Optional[float] = None

    for line in _strip_prefix(raw, "IOSTAT-"):
        ts = _TIMESTAMP_RE.match(line.strip())
        if ts:
            now = _parse_timestamp(*ts.groups())
            if now is not None and prev_ts is not None and now > prev_ts:
                sample_secs = now - prev_ts
            prev_ts = now
            continue
        fields = line.split()
        if not fields:
            continue
        if fields[0].rstrip(":") == "Device":
            columns = {name: i for i, name in enumerate(fields)}
            continue
        if columns is None or len(fields) != len(columns):
            continue
        try:
            stat = _iostat_sample(fields, columns)
        except (KeyError, ValueError):
            continue
        d = devices.setdefault(fields[0], {
            "readMiB": 0.0, "writeMiB": 0.0, "readIos": 0.0, "writeIos": 0.0,
            "_rAwaitSum": 0.0, "_wAwaitSum": 0.0, "maxUtilPct": 0.0, "samples": 0,
        })
        d["readMiB"]    += stat["r_mb_s"] * sample_secs
        d["writeMiB"]   += stat["w_mb_s"] * sample_secs
        d["readIos"]    += stat["r_s"] * sample_secs
        d["writeIos"]   += stat["w_s"] * sample_secs
        d["_rAwaitSum"] += stat["r_await"] * stat["r_s"] * sample_secs
        d["_wAwaitSum"] += stat["w_await"] * stat["w_s"] * sample_secs
        d["maxUtilPct"]  = max(d["maxUtilPct"], stat["util"])
        d["samples"]    += 1

    for d in devices.values():
        r_await, w_await = d.pop("_rAwaitSum"), d.pop("_wAwaitSum")
        d["readAwaitMs"]  = round(r_await / d["readIos"], 3) if d["readIos"] else 0.0
        d["writeAwaitMs"] = round(w_await / d["writeIos"], 3) if d["writeIos"] else 0.0
        for k in ("readMiB", "writeMiB", "readIos", "writeIos"):
            d[k] = round(d[k], 2)
    return devices


def _iostat_sample(fields: list[str], columns: dict) -> dict:
    """One device line → rates, in MB/s whichever unit iostat printed."""
    def col(*names):
        for n in names:
            if n in columns:
                return float(fields[columns[n]])
        raise KeyError(names)

    if "rMB/s" in columns:
        r_mb, w_mb = col("rMB/s"), col("wMB/s")
    else:
        r_mb, w_mb = col("rkB/s") / 1024, col("wkB/s") / 1024
    return {
        "r_s": col("r/s"), "w_s": col("w/s"),
        "r_mb_s": r_mb, "w_mb_s": w_mb,
        "r_await": col("r_await"), "w_await": col("w_await"),
        "util": col("%util"),
    }


# ── ifconfig parser ───────────────────────────────────────────────────────────

_IFACE_RE = re.compile(r"^(\S+?):\s+flags=")
_IFBYTES_RE = re.compile(r"(RX|TX) packets \d+\s+bytes (\d+)")


def parse_ifconfig_deltas(raw: str) -> dict:
    """
    Bytes each interface received and sent between the collector's
    IFCONFIG.BEFORE- and IFCONFIG.AFTER- snapshots:
      {interface: {"rxBytes": int, "txBytes": int}}
    """
    def snapshot(prefix):
        counters, iface = {}, None
        for line in _strip_prefix(raw, prefix) if prefix in raw else []:
            m = _IFACE_RE.match(line)
            if m:
                iface = m.group(1)
                continue
            m = _IFBYTES_RE.search(line)
            if m and iface:
                counters.setdefault(iface, {})[m.group(1)] = int(m.group(2))
        return counters

    before, after = snapshot("IFCONFIG.BEFORE-"), snapshot("IFCONFIG.AFTER-")
    deltas = {}
    for iface, end in after.items():
        start = before.get(iface)
        if not start or "RX" not in end or "TX" not in end:
            continue
        deltas[iface] = {"rxBytes": max(0, end["RX"] - start.get("RX", 0)),
                         "txBytes": max(0, end["TX"] - start.get("TX", 0))}
    return deltas


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

    text = "\n".join(_strip_prefix(raw, "VMSTAT-"))
    wa_values = [
        float(m.group("wa"))
        for m in VMSTAT_PATTERN.finditer(text)
    ]

    if not wa_values:
        return {}

    return {
        "avgIoWaitPct": round(sum(wa_values) / len(wa_values), 2),
        "maxIoWaitPct": round(max(wa_values), 2),
    }


# ── Combined storage stats ────────────────────────────────────────────────────

def parse_storage_stats(
    node_logs: dict[str, str],
    runtime_secs: float,
    interval: float = 10.0,
    device_pattern: Optional[str] = None,
    network_interfaces: Optional[list[str]] = None,
) -> dict:
    """
    Storage and network metrics for a run from each node's collector log.

    node_logs: {node name: stats collector log}
    runtime_secs: the benchmark's measured duration; rates are the totals
      divided by it (the collection window is longer and partly idle, so
      dividing by the window would understate them).
    device_pattern: regex for the block devices to count
      (default: NVMe namespace heads, DEFAULT_DEVICE_PATTERN).
    network_interfaces: interfaces to total for net* metrics; per-interface
      bytes for every interface are in storageByNode either way.

    Result keys (cluster-wide sums over the selected devices / interfaces):
      storageReadMiB, storageWriteMiB, storageReadIos, storageWriteIos,
      storageReadMiBs, storageWriteMiBs, storageReadIops, storageWriteIops,
      storageReadAwaitMs, storageWriteAwaitMs (I/O-weighted),
      netRxMiB, netTxMiB, netRxMiBs, netTxMiBs,
      avgIoWaitPct, maxIoWaitPct, storageByNode (per node detail).
    """
    pattern = re.compile(device_pattern or DEFAULT_DEVICE_PATTERN)
    wanted_ifaces = set(network_interfaces or [])
    totals = {"readMiB": 0.0, "writeMiB": 0.0, "readIos": 0.0, "writeIos": 0.0,
              "rAwait": 0.0, "wAwait": 0.0, "rx": 0, "tx": 0}
    by_node, iowait = {}, []

    for node, raw in node_logs.items():
        devices = {d: v for d, v in parse_iostat_devices(raw, interval).items()
                   if pattern.search(d)}
        net = parse_ifconfig_deltas(raw)
        by_node[node] = {"devices": devices, "network": net}
        for v in devices.values():
            totals["readMiB"]  += v["readMiB"]
            totals["writeMiB"] += v["writeMiB"]
            totals["readIos"]  += v["readIos"]
            totals["writeIos"] += v["writeIos"]
            totals["rAwait"]   += v["readAwaitMs"] * v["readIos"]
            totals["wAwait"]   += v["writeAwaitMs"] * v["writeIos"]
        for iface in wanted_ifaces & set(net):
            totals["rx"] += net[iface]["rxBytes"]
            totals["tx"] += net[iface]["txBytes"]
        vm = parse_vmstat(raw)
        if vm:
            iowait.append(vm)

    if not any(n["devices"] or n["network"] for n in by_node.values()):
        logger.warning("No iostat or ifconfig data found in stats output")
        return {}

    secs = max(float(runtime_secs), 1.0)
    mib = 1024 * 1024
    result = {
        "storageReadMiB":   round(totals["readMiB"], 2),
        "storageWriteMiB":  round(totals["writeMiB"], 2),
        "storageReadIos":   round(totals["readIos"]),
        "storageWriteIos":  round(totals["writeIos"]),
        "storageReadMiBs":  round(totals["readMiB"] / secs, 2),
        "storageWriteMiBs": round(totals["writeMiB"] / secs, 2),
        "storageReadIops":  round(totals["readIos"] / secs, 2),
        "storageWriteIops": round(totals["writeIos"] / secs, 2),
        "storageReadAwaitMs":  round(totals["rAwait"] / totals["readIos"], 3)
                               if totals["readIos"] else 0.0,
        "storageWriteAwaitMs": round(totals["wAwait"] / totals["writeIos"], 3)
                               if totals["writeIos"] else 0.0,
        "storageByNode": by_node,
    }
    if wanted_ifaces:
        result.update({
            "netRxMiB":  round(totals["rx"] / mib, 2),
            "netTxMiB":  round(totals["tx"] / mib, 2),
            "netRxMiBs": round(totals["rx"] / mib / secs, 2),
            "netTxMiBs": round(totals["tx"] / mib / secs, 2),
        })
    if iowait:
        result["avgIoWaitPct"] = round(sum(v["avgIoWaitPct"] for v in iowait) / len(iowait), 2)
        result["maxIoWaitPct"] = max(v["maxIoWaitPct"] for v in iowait)
    return result


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
