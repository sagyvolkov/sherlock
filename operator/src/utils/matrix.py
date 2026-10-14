"""
matrix.py — Parameter sweep expansion.

Converts a spec.sweep dict (where each value is a list) into a flat list of
parameter dicts representing the cartesian product of all axes.

This is the Python equivalent of the nested for-loops in the original
sherlock bash scripts (run_loops, run_fio_tests, etc.).

Example:
    sweep = {
        "virtualUsers": [8, 16, 32],
        "warehouses":   [100],
        "testMins":     [5, 10],
    }
    expand(sweep) → [
        {"virtualUsers": 8,  "warehouses": 100, "testMins": 5},
        {"virtualUsers": 8,  "warehouses": 100, "testMins": 10},
        {"virtualUsers": 16, "warehouses": 100, "testMins": 5},
        {"virtualUsers": 16, "warehouses": 100, "testMins": 10},
        {"virtualUsers": 32, "warehouses": 100, "testMins": 5},
        {"virtualUsers": 32, "warehouses": 100, "testMins": 10},
    ]
"""

import hashlib
import itertools
from typing import Any, Optional


def expand(sweep: dict) -> list[dict]:
    """
    Expand a sweep spec dict into a list of parameter combination dicts.

    Fields whose value is a list with more than one entry become sweep axes.
    Fields with a single-element list are treated as fixed params but are
    still included in every output dict for completeness.

    Non-list values (e.g. a plain int or string) are treated as fixed params.
    """
    if not sweep:
        return [{}]

    keys = []
    value_lists = []

    for key, value in sweep.items():
        keys.append(key)
        if isinstance(value, list):
            value_lists.append(value)
        else:
            # Scalar value — wrap in a list so itertools.product works
            value_lists.append([value])

    combinations = list(itertools.product(*value_lists))
    return [dict(zip(keys, combo)) for combo in combinations]



_NAME_EXCLUDE = frozenset({
    "groupReporting",
    "outputFormat",
    "reportInterval",
})

def _should_include_in_name(key: str, value) -> bool:
    if key in _NAME_EXCLUDE:
        return False
    if isinstance(value, bool):
        return False
    return True


def make_run_name(suite_name: str, params: dict, max_len: int = 60,
                  first_keys: Optional[list] = None) -> str:
    """
    Generate a deterministic run name from the suite name and parameter values.

    Mirrors the original bash naming convention:
      ${RUN_NAME}-s${s}-t${t}-upd${update_pct}-rt${run_time}

    Each parameter is encoded as <abbreviated_key><value>.
    Examples:
      mssql-vu-sweep + {virtualUsers:16, testMins:5} → mssql-vu-sweep-vu16-tm5
      nvme-sweep + {blockSize:"4k", rwMixWrite:0}    → nvme-sweep-bs4k-rw0

    The abbreviation map handles known parameter names. Unknown parameters
    fall back to the full key name.

    first_keys: parameters to encode first, normally the ones that differ
    between the suite's runs, so they survive truncation. A name longer than
    max_len is cut and ends with a hash of the full name, which keeps it
    unique (it used to end in "xxx", so runs differing only in the cut-off
    part got the same name).
    """
    ABBREV = {
        # HammerDB
        "virtualUsers":  "vu",
        "warehouses":    "wh",
        "rampupMins":    "rm",
        "testMins":      "tm",
        # pgbench
        "clients":       "c",
        "threads":       "t",
        "readWriteRatio": "rw",
        "scaleFactor":   "sf",
        "duration":      "d",
        "protocol":      "p",
        # sysbench
        # threads → t (shared with pgbench)
        "tables":        "tbl",
        "tableSize":     "ts",
        "testType":      "tt",
        # YCSB
        "workload":      "wl",
        "recordCount":   "rc",
        "operationCount": "oc",
        "requestDistribution": "dist",
        "maxExecutionTime": "met",
        # sysbench
        "reportInterval": "ri",
        # fio
        "blockSize":     "bs",
        "rwMixWrite":    "rw",
        "ioPattern":     "io",
        "runtime":       "rt",
        "jobs":          "j",
        "ioDepth":       "qd",
        "workSize":      "ws",
        "directIO":      "dio",
    }

    first = [k for k in (first_keys or []) if k in params]
    ordered = first + [k for k in params if k not in first]
    parts = [suite_name]
    for key in ordered:
        value = params[key]
        if not _should_include_in_name(key, value):
            continue
        abbrev = ABBREV.get(key, key)
        # Clean the value: remove non-alphanumeric chars except letters/digits
        clean_value = str(value).replace(" ", "").replace("_", "")
        parts.append(f"{abbrev}{clean_value}")

    name = "-".join(parts)

    # k8s names must be <= 63 chars (DNS label). Truncate if needed,
    # keeping the suite name prefix intact.
    name = name.lower()
    if len(name) > max_len:
        digest = hashlib.sha1(name.encode()).hexdigest()[:8]
        name = name[: max_len - len(digest) - 1].rstrip("-") + "-" + digest

    return name


def varying_keys(matrix: list[dict]) -> list[str]:
    """Parameters whose value differs between runs, in first-seen order."""
    keys = []
    for params in matrix:
        for k in params:
            if k not in keys and len({repr(p.get(k)) for p in matrix}) > 1:
                keys.append(k)
    return keys


def count_runs(sweep: dict) -> int:
    """Return total number of runs the sweep will generate."""
    return len(expand(sweep))
