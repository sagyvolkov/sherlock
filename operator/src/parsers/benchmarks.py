"""
parsers/benchmarks.py — Benchmark-specific result parsers.

Each function parses the raw stdout from a benchmark container pod log
and returns a structured dict matching the CRD status.runMatrix[].result schema.

These replace the print_results bash script functions:
  print_sysbench_results_per_single_run()
  print_hammerdb_results_per_single_run()
  print_ycsb_results_per_single_run()
  (pgbench parsing was done inline with awk in the original)

All parsers follow the same interface:
  parse(raw_output: str, params: dict) -> dict

The returned dict keys match the result fields in the CRD status schemas.
"""

import re
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Strip ANSI escape codes before parsing (some benchmark tools emit colours)
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


def _clean(raw: str) -> str:
    return _ANSI_RE.sub('', raw)


# ── pgbench ───────────────────────────────────────────────────────────────────

def parse_pgbench(raw: str, params: dict) -> dict:
    """
    Parse pgbench output.

    pgbench -T 120 produces a final summary like:
      transaction type: <builtin: TPC-B (sort of)>
      scaling factor: 100
      number of clients: 8
      number of threads: 4
      duration: 120 s
      number of transactions actually processed: 123456
      latency average = 7.804 ms
      latency stddev = 3.221 ms
      tps = 1028.786482 (including connections establishing)
      tps = 1029.123456 (excluding connections establishing)

    We use the 'excluding connections establishing' TPS as the primary metric,
    matching what the original sherlock scripts reported.
    """
    text = _clean(raw)
    result = {}

    # TPS — prefer 'excluding connections establishing'
    tps_matches = re.findall(
        r'tps\s*=\s*([\d.]+)\s*\(excluding', text
    )
    if not tps_matches:
        tps_matches = re.findall(r'tps\s*=\s*([\d.]+)', text)
    if tps_matches:
        # If multiple DB instances, sum them (each logs separately)
        result["tps"] = round(sum(float(v) for v in tps_matches), 2)

    # Latency average
    lat_avg = re.search(r'latency average\s*=\s*([\d.]+)\s*ms', text)
    if lat_avg:
        result["latencyAvgMs"] = float(lat_avg.group(1))

    # Latency stddev
    lat_std = re.search(r'latency stddev\s*=\s*([\d.]+)\s*ms', text)
    if lat_std:
        result["latencyStddevMs"] = float(lat_std.group(1))

    if not result:
        logger.warning("pgbench parser: no metrics found in output")

    return result


# ── sysbench ──────────────────────────────────────────────────────────────────

def parse_sysbench(raw: str, params: dict) -> dict:
    """
    Parse sysbench oltp_* output.

    sysbench produces output like:
      SQL statistics:
          queries performed:
              read:     1234567
              write:    345678
              ...
          transactions:       12345  (102.87 per sec.)
          queries:            234567 (1955.00 per sec.)
      ...
      Latency (ms):
               min:     0.58
               avg:     1.55
               max:     45.23
               95th percentile: 3.19
               sum:     19345678
      ...

    Replaces print_sysbench_results_per_single_run() which parsed:
      grep 'transactions|avg:|percentile:' | paste -d "@" - - -
      and extracted values with awk.
    """
    text = _clean(raw)
    result = {}

    # TPS — "transactions: N (M per sec.)"
    tps_matches = re.findall(
        r'transactions:\s+\d+\s+\(([\d.]+)\s+per sec\.\)', text
    )
    if tps_matches:
        result["tps"] = round(sum(float(v) for v in tps_matches), 2)

    # QPS — "queries: N (M per sec.)"
    qps_matches = re.findall(
        r'queries:\s+\d+\s+\(([\d.]+)\s+per sec\.\)', text
    )
    if qps_matches:
        result["qps"] = round(sum(float(v) for v in qps_matches), 2)

    # Average latency
    lat_avg = re.findall(r'avg:\s+([\d.]+)', text)
    if lat_avg:
        result["latencyAvgMs"] = round(
            sum(float(v) for v in lat_avg) / len(lat_avg), 2
        )

    # 95th percentile latency — sysbench reports this natively
    lat_95 = re.findall(r'95th percentile:\s+([\d.]+)', text)
    if lat_95:
        result["latencyP95Ms"] = round(
            sum(float(v) for v in lat_95) / len(lat_95), 2
        )

    # Max latency
    lat_max = re.findall(r'max:\s+([\d.]+)', text)
    if lat_max:
        result["latencyMaxMs"] = round(max(float(v) for v in lat_max), 2)

    if not result:
        logger.warning("sysbench parser: no metrics found in output")

    return result


# ── HammerDB ──────────────────────────────────────────────────────────────────

def parse_hammerdb(raw: str, params: dict) -> dict:
    """
    Parse HammerDB TPC-C output.

    HammerDB prints a result line like:
      System achieved 45231 NOPM from 125678 SQL Server TPM

    Replaces print_hammerdb_results_per_single_run() which used:
      grep 'System achieved' ${run_dir}/${BENCHMARK_TOOL}-*.log
      awk '{print $7}' for TPM, awk '{print $12}' for NOPM
    """
    text = _clean(raw)

    # Find all "System achieved N NOPM from M SQL Server TPM" lines
    HAMMERDB_RE = re.compile(
        r'System achieved\s+([\d,]+)\s+NOPM\s+from\s+([\d,]+)\s+.+?\s+TPM'
    )
    matches = HAMMERDB_RE.findall(text)

    if not matches:
        logger.warning("hammerdb parser: no 'System achieved' line found")
        return {}

    total_nopm = sum(int(m[0].replace(",", "")) for m in matches)
    total_tpm  = sum(int(m[1].replace(",", "")) for m in matches)
    n = len(matches)

    return {
        "nopm": round(total_nopm / n, 2),
        "tpm":  round(total_tpm  / n, 2),
    }


# ── YCSB ─────────────────────────────────────────────────────────────────────

def parse_ycsb(raw: str, params: dict) -> dict:
    """
    Parse YCSB output.

    YCSB produces lines like:
      [OVERALL], Throughput(ops/sec), 12345.67
      grep 'Throughput|[READ], AverageLatency(us), 234.56
      grep 'Throughput|[READ], 95thPercentileLatency(us), 456.78
      grep 'Throughput|[READ], 99thPercentileLatency(us), 789.01
      [UPDATE], AverageLatency(us), 345.67
      [UPDATE], 95thPercentileLatency(us), 567.89
      [UPDATE], 99thPercentileLatency(us), 890.12

    Replaces print_ycsb_results_per_single_run() which used:
      grep 'Throughput|[READ], AverageLatency|...' | paste -d "@" - - - - -
      and awk -F, '{print $3}'

    Note: YCSB latency is in microseconds (us) not milliseconds.
    The CRD result fields use 'Us' suffix to make this explicit.
    """
    text = _clean(raw)
    result = {}

    def _find_ycsb_metric(pattern: str) -> Optional[float]:
        """Find a YCSB CSV-format metric value."""
        m = re.search(pattern, text)
        return float(m.group(1)) if m else None

    # Overall throughput
    ops = _find_ycsb_metric(
        r'\[OVERALL\],\s*Throughput\(ops/sec\),\s*([\d.]+)'
    )
    if ops is not None:
        result["throughputOpsPerSec"] = ops

    # Read latencies (microseconds)
    r_avg = _find_ycsb_metric(r'\[READ\],\s*AverageLatency\(us\),\s*([\d.]+)')
    r_95  = _find_ycsb_metric(r'\[READ\],\s*95thPercentileLatency\(us\),\s*([\d.]+)')
    r_99  = _find_ycsb_metric(r'\[READ\],\s*99thPercentileLatency\(us\),\s*([\d.]+)')
    if r_avg is not None: result["readLatencyAvgUs"]  = r_avg
    if r_95  is not None: result["readLatencyP95Us"]  = r_95
    if r_99  is not None: result["readLatencyP99Us"]  = r_99

    # Update/Write latencies (microseconds)
    w_avg = _find_ycsb_metric(r'\[UPDATE\],\s*AverageLatency\(us\),\s*([\d.]+)')
    w_95  = _find_ycsb_metric(r'\[UPDATE\],\s*95thPercentileLatency\(us\),\s*([\d.]+)')
    w_99  = _find_ycsb_metric(r'\[UPDATE\],\s*99thPercentileLatency\(us\),\s*([\d.]+)')
    if w_avg is not None: result["updateLatencyAvgUs"] = w_avg
    if w_95  is not None: result["updateLatencyP95Us"] = w_95
    if w_99  is not None: result["updateLatencyP99Us"] = w_99

    if not result:
        logger.warning("ycsb parser: no metrics found in output")

    return result


# ── fio ───────────────────────────────────────────────────────────────────────

def parse_fio(raw: str, params: dict) -> dict:
    """
    Parse fio --output-format=json output.

    fio JSON output structure (abbreviated):
      {
        "jobs": [{
          "read": {
            "iops": 12345.67,
            "bw": 50000,          // kB/s
            "lat_ns": {
              "mean": 234567,     // nanoseconds
              "percentile": {
                "50.000000": 200000,
                "95.000000": 450000,
                "99.000000": 780000,
                "99.900000": 1200000
              }
            }
          },
          "write": { ... same structure ... }
        }]
      }

    fio with --group_reporting aggregates across all jobs in one entry.
    The original fio container used --output-format=normal; v2 uses json
    (set in the CRD outputFormat field) for cleaner parsing.

    Falls back to parsing 'normal' format for backward compatibility.
    """
    text = _clean(raw)

    # Try JSON first
    try:
        # fio JSON output may have non-JSON preamble; find the first '{'
        json_start = text.find('{')
        if json_start >= 0:
            data = json.loads(text[json_start:])
            return _parse_fio_json(data)
    except (json.JSONDecodeError, KeyError) as e:
        logger.debug(f"fio JSON parse failed ({e}), falling back to normal format")

    # Fallback: parse fio 'normal' / 'minimal' output
    return _parse_fio_normal(text)


def _parse_fio_json(data: dict) -> dict:
    """Parse fio --output-format=json output."""
    result = {}
    jobs = data.get("jobs", [])
    if not jobs:
        return result

    # With --group_reporting there's one job entry aggregating all threads
    # Sum across jobs if multiple (one per worker — fio running in parallel)
    total_r_iops = sum(j["read"]["iops"]   for j in jobs)
    total_w_iops = sum(j["write"]["iops"]  for j in jobs)
    total_r_bw   = sum(j["read"]["bw"]     for j in jobs)  # kB/s
    total_w_bw   = sum(j["write"]["bw"]    for j in jobs)

    result["readIops"]    = round(total_r_iops, 2)
    result["writeIops"]   = round(total_w_iops, 2)
    result["totalIops"]   = round(total_r_iops + total_w_iops, 2)
    result["readBwMBs"]   = round(total_r_bw / 1024, 2)   # kB/s → MB/s
    result["writeBwMBs"]  = round(total_w_bw / 1024, 2)
    result["totalBwMBs"]  = round((total_r_bw + total_w_bw) / 1024, 2)

    # Latency from first job (percentiles are per-job in fio JSON)
    # lat_ns is completion latency; clat_ns is completion latency without submission
    # We use lat_ns to match what users care about end-to-end
    def _ns_to_us(ns: float) -> float:
        return round(ns / 1000, 2)

    j0 = jobs[0]
    for rw, key_prefix in [("read", "read"), ("write", "write")]:
        lat = j0.get(rw, {}).get("lat_ns", {})
        mean = lat.get("mean", 0)
        pct  = lat.get("percentile", {})
        if mean:
            result[f"{key_prefix}LatAvgUs"] = _ns_to_us(mean)
        if "50.000000"  in pct: result[f"{key_prefix}LatP50Us"]  = _ns_to_us(pct["50.000000"])
        if "95.000000"  in pct: result[f"{key_prefix}LatP95Us"]  = _ns_to_us(pct["95.000000"])
        if "99.000000"  in pct: result[f"{key_prefix}LatP99Us"]  = _ns_to_us(pct["99.000000"])
        if "99.900000"  in pct: result[f"{key_prefix}LatP999Us"] = _ns_to_us(pct["99.900000"])

    return result


def _parse_fio_normal(text: str) -> dict:
    """
    Fallback parser for fio --output-format=normal (original sherlock v1 format).
    Extracts IOPS and bandwidth from lines like:
      read: IOPS=12.3k, BW=49.2MiB/s (51.6MB/s)
      write: IOPS=4567, BW=17.8MiB/s (18.7MB/s)
    """
    result = {}

    def _parse_iops(s: str) -> float:
        """Convert '12.3k' or '1234' to float."""
        s = s.strip()
        if s.endswith('k'):
            return float(s[:-1]) * 1000
        elif s.endswith('M'):
            return float(s[:-1]) * 1_000_000
        return float(s)

    def _parse_bw_mb(s: str) -> float:
        """Parse bandwidth string like '49.2MiB/s' or '51.6MB/s' to MB/s."""
        m = re.match(r'([\d.]+)(MiB|MB|KiB|KB|GiB|GB)', s)
        if not m:
            return 0.0
        val, unit = float(m.group(1)), m.group(2)
        if unit in ("KiB", "KB"):   return val / 1024
        if unit in ("GiB", "GB"):   return val * 1024
        if unit == "MiB":           return val * 1.048576   # MiB to MB
        return val

    for rw, prefix in [("read", "read"), ("write", "write")]:
        m = re.search(
            rf'{rw}:\s+IOPS=([\d.]+[kMG]?),\s+BW=([\d.]+(?:MiB|MB|KiB|KB)\/s)',
            text, re.IGNORECASE
        )
        if m:
            result[f"{prefix}Iops"]  = round(_parse_iops(m.group(1)), 2)
            result[f"{prefix}BwMBs"] = round(_parse_bw_mb(m.group(2)), 2)

    if "readIops" in result and "writeIops" in result:
        result["totalIops"]  = round(result["readIops"]  + result["writeIops"],  2)
        result["totalBwMBs"] = round(result["readBwMBs"] + result["writeBwMBs"], 2)

    return result
