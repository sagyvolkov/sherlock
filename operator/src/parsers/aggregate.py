"""
parsers/aggregate.py — Combine per-instance benchmark results into one run result.

A run launches one benchmark Job per database instance (or per PVC for fio).
Each Job's log is parsed on its own by the benchmark parser, and the results
are combined here into the run-level result stored in status.runMatrix[].result.

Rules, chosen by metric name so every benchmark parser works unchanged:
  - Throughput (tps, qps, nopm, tpm, ops/sec, IOPS, bandwidth): summed.
    The run's throughput is the cluster-wide total.
  - Average latencies (keys containing "Avg"): mean weighted by each
    instance's throughput, which equals the per-operation mean when all
    instances ran for the same duration. fio's readLat*/writeLat* use that
    direction's IOPS as the weight.
  - latencyStddevMs: pooled standard deviation across instances, using the
    matching latencyAvgMs values and throughput weights.
  - Percentiles and maxima (P50/P95/P99/P999, "Max"): the worst instance.
    Percentiles cannot be combined exactly from per-instance summaries, so
    the run reports the slowest instance instead of an invented number.
  - Anything else: plain mean.
"""

import math
import re

# Summed across instances
_SUM_KEYS = {
    "tps", "qps", "nopm", "tpm", "throughputOpsPerSec",
    "readIops", "writeIops", "totalIops",
    "readBwMBs", "writeBwMBs", "totalBwMBs",
    "readMiB", "writeMiB", "readIos", "writeIos",
}

# Used, in this order, as each instance's weight for averaged latencies
_WEIGHT_KEYS = ("tps", "nopm", "throughputOpsPerSec", "totalIops", "qps")

_PERCENTILE_RE = re.compile(r"P\d+")


def aggregate_results(per_instance: list[dict]) -> dict:
    """
    Combine a list of per-instance result dicts into one result dict.

    Instances with an empty result are ignored. Returns {} if none have data.
    """
    results = [r for r in per_instance if r]
    if not results:
        return {}
    if len(results) == 1:
        return dict(results[0])

    weights = [_weight(r) for r in results]
    keys = []
    for r in results:
        for k in r:
            if k not in keys:
                keys.append(k)

    combined = {}
    for key in keys:
        present = [(r[key], w) for r, w in zip(results, weights)
                   if isinstance(r.get(key), (int, float))]
        if not present:
            continue
        values = [v for v, _ in present]

        if key in _SUM_KEYS:
            combined[key] = round(sum(values), 2)
        elif _PERCENTILE_RE.search(key) or "Max" in key:
            combined[key] = round(max(values), 3)
        elif key == "latencyStddevMs":
            combined[key] = _pooled_stddev(results, weights)
        elif "Avg" in key:
            combined[key] = _weighted_mean(_reweight_by_direction(key, results, present))
        else:
            combined[key] = round(sum(values) / len(values), 3)

    return combined


def _weight(result: dict) -> float:
    for key in _WEIGHT_KEYS:
        value = result.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return 1.0


def _reweight_by_direction(key: str, results: list[dict],
                           present: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Use readIops / writeIops as the weight for fio read*/write* latencies."""
    direction = next((d for d in ("read", "write") if key.startswith(d)), None)
    if direction is None:
        return present
    rows = [(r[key], r.get(f"{direction}Iops")) for r in results
            if isinstance(r.get(key), (int, float))]
    if all(isinstance(w, (int, float)) for _, w in rows):
        return [(v, float(w)) for v, w in rows]
    return present


def _weighted_mean(present: list[tuple[float, float]]) -> float:
    total_w = sum(w for _, w in present)
    if total_w <= 0:
        return round(sum(v for v, _ in present) / len(present), 3)
    return round(sum(v * w for v, w in present) / total_w, 3)


def _pooled_stddev(results: list[dict], weights: list[float]) -> float:
    """
    Pooled standard deviation of latency across instances:
      sqrt( sum w_i * (sd_i^2 + (mean_i - mean)^2) / sum w_i )
    Instances missing either latencyAvgMs or latencyStddevMs are skipped.
    """
    rows = [
        (r["latencyAvgMs"], r["latencyStddevMs"], w)
        for r, w in zip(results, weights)
        if isinstance(r.get("latencyAvgMs"), (int, float))
        and isinstance(r.get("latencyStddevMs"), (int, float))
    ]
    if not rows:
        return 0.0
    total_w = sum(w for _, _, w in rows)
    mean = sum(m * w for m, _, w in rows) / total_w
    var = sum(w * (sd * sd + (m - mean) ** 2) for m, sd, w in rows) / total_w
    return round(math.sqrt(var), 3)
