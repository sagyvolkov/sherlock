# Adding a new benchmark to Sherlock

This guide walks through everything required to add a new benchmark or database
to Sherlock v2. By the time you're done you'll have a new CRD, a container image,
operator handlers, a stat parser, and example manifests.

Use fio as the reference implementation for a pure storage benchmark, and
pgbench as the reference for a database benchmark.

---

## Overview of what needs to be added

```
operator/
  crds/
    benchmarks/
      <name>-suite.yaml         ← 1. CRD schema
    examples/
      <name>-example.yaml       ← 2. Example manifests
containers/
  <name>_container/
    Dockerfile                  ← 3. Benchmark container image
    runs/
      run_<name>                ← 4. Benchmark entrypoint script (Bash, inside container)
operator/
  handlers/
    <name>.py                   ← 5. kopf operator handler
  parsers/
    <name>.py                   ← 6. Python result parser
  plugins/
    storage/
      generic.py                ← (shared, no change needed usually)
```

---

## Step 1 — CRD schema

Create `operator/crds/benchmarks/<name>-suite.yaml`.

Every Sherlock Suite CRD has four required top-level sections in `spec`:

### `spec.database` (DB benchmarks) or `spec.storage` (storage-only benchmarks)

For a **database benchmark** (PostgreSQL, MySQL, MongoDB, MSSQL):
- Copy the `database` block from `pgbench-suite.yaml` or `hammerdb-suite.yaml`
- Change `type` enum to include your database engine(s)
- Add any database-specific fields (e.g. `partitionedTables` for pgbench)
- Keep `storageClass`, `storageSize`, `storagePlugin`, `resources`, `nodeSelector`,
  `tolerations` identical — these are shared infrastructure fields

For a **storage-only benchmark** (like fio):
- Copy the `storage` block from `fio-suite.yaml`
- There is no database layer, so `volumeMode` and `fioPerWorker` replace
  `instancesPerNode`

### `spec.execution`

Copy verbatim from any existing CRD. This block is identical across all Suite kinds:
```yaml
execution:
  order: sequential | parallel
  sleepBetweenRuns: 30
  failurePolicy: abortAll | retryN | continueAndReport
  retryLimit: 3
  timeoutPerRun: 1h
```

### `spec.sweep`

This is the benchmark-specific section. Rules:
- Every field accepts a **list of values** (`type: array`)
- Multi-value lists become sweep axes; single-value lists are fixed params
- Field names should map directly to the benchmark's CLI flags or config keys
- Add a `description` to every field explaining the CLI flag it maps to
- Include an `example:` annotation showing realistic values

Checklist for sweep fields:
- [ ] Primary scaling axis (threads, clients, virtual users, block size)
- [ ] Workload type or mix (read/write ratio, test script, workload letter)
- [ ] Dataset or work size
- [ ] Duration / operation count
- [ ] Any benchmark-specific tuning knobs worth sweeping

### `spec.artifacts`

Copy verbatim from any existing CRD. This block is identical across all Suite kinds.

### `status` block

Required fields (copy from any existing CRD):
- `phase` — add any benchmark-specific phases if needed
  (fio adds `Provisioning`; most DB benchmarks don't need extras)
- `observedGeneration`
- `runMatrix[]` with `name`, `params`, `phase`, `startTime`, `completionTime`,
  `retryCount`, `artifactPath`, `result`
- `summary` with `totalRuns`, `runsCompleted`, `runsFailed`, `runsRemaining`
- `artifacts` with `pvcName`, `bundles[]`
- `conditions[]` — use `PVCsReady` for storage benchmarks,
  `DatabaseReady` for DB benchmarks (or add a new type if appropriate)

**The `result` object is benchmark-specific.** Add fields for:
1. The benchmark's primary output metric(s) — e.g. TPS, IOPS, ops/sec, NOPM
2. Any secondary metrics the benchmark natively reports (latency percentiles, etc.)
3. The shared storage metrics block (copy from any existing CRD) — these come
   from the stat collectors, not the benchmark itself, and enable cross-suite comparison:
   ```yaml
   avgReadIops:   {type: number}
   avgWriteIops:  {type: number}
   avgReadBwMBs:  {type: number}
   avgWriteBwMBs: {type: number}
   avgLatencyMs:  {type: number}
   p95LatencyMs:  {type: number}
   p99LatencyMs:  {type: number}
   ```

### CRD naming conventions

| Thing | Pattern | Example |
|-------|---------|---------|
| CRD filename | `<name>-suite.yaml` | `pgbench-suite.yaml` |
| CRD metadata.name | `sherlock<name>suites.sherlock.io` | `sherlockpgbenchsuites.sherlock.io` |
| Kind | `Sherlock<Name>Suite` | `SherlockPgbenchSuite` |
| shortNames | `sl<abbrev>` | `slpg`, `slpgbench` |

---

## Step 2 — Example manifests

Create `operator/crds/examples/<name>-example.yaml`.

Good examples include:
- A header comment explaining what the sweep produces (N × M = total runs)
- A comment showing what the run names will look like
- At least one `pvc` destination example
- Ideally a second example showing a different destination (`s3` or `local`)
- Realistic values — don't use toy numbers, use values that would actually be
  useful for storage characterization

---

## Step 3 — Benchmark container image

Create `containers/<name>_container/`.

Minimum contents:
```
containers/<name>_container/
  Dockerfile
  runs/
    run_<name>       ← entrypoint: accepts parameters, runs benchmark, writes output
```

The container must:
- Accept all sweep parameters as positional arguments or environment variables
- Write output to stdout (the operator captures logs) AND to a file under `/output/`
- Exit 0 on success, non-zero on failure (the operator watches exit codes)
- Not require network access beyond the database/storage being tested
- Be published to `quay.io/sagyvolkov/<name>_container:<version>`

For the entrypoint script, follow the existing containers:
- `containers/fio_container/runs/run_fio` — simple, positional args, calls the tool
- `containers/pgbench/` — two-phase (init + run)
- `containers/hammerdb/` — TCL script generation then HammerDB execution

Output format: default to JSON where the benchmark supports it (fio, sysbench).
For tools without JSON output (pgbench, hammerdb), structured text is fine —
the parser (Step 6) handles the extraction.

---

## Step 4 — Benchmark entrypoint (inside container)

The `run_<name>` script inside the container should:

1. Parse arguments
2. Run any setup phase (create tables, load data, warmup)
3. Run the benchmark
4. Write structured output to `/output/<run_name>.json` or `/output/<run_name>.log`
5. Print a summary to stdout with at minimum the primary metric value

Keep it simple — the container script is not responsible for orchestration,
waiting, or collecting stats. The operator and stat collectors handle those.

---

## Step 5 — kopf operator handler

Create `operator/handlers/<name>.py`.

The handler registers three kopf event handlers for your new Suite kind:

```python
import kopf
from operator.matrix import expand_cartesian
from operator.runners import run_benchmark_job, collect_artifacts
from operator.parsers import <name> as parser

@kopf.on.create('sherlock.io', 'v1alpha1', 'Sherlock<Name>Suite')
def on_create(spec, name, namespace, patch, **kwargs):
    """
    Called when a new SherlockXxxSuite CR is created.
    Responsibilities:
    1. Expand spec.sweep into the run matrix
    2. Write matrix to status.runMatrix
    3. Create database/storage infrastructure (PVCs, DB pods)
    4. Set status.phase = Deploying
    """
    matrix = expand_cartesian(spec['sweep'])
    patch.status['runMatrix'] = [{'name': make_run_name(name, p), 'params': p,
                                   'phase': 'Pending'} for p in matrix]
    patch.status['phase'] = 'Deploying'
    patch.status['summary'] = {'totalRuns': len(matrix), 'runsCompleted': 0,
                                'runsFailed': 0, 'runsRemaining': len(matrix)}

@kopf.on.field('sherlock.io', 'v1alpha1', 'Sherlock<Name>Suite',
               field='status.phase')
def on_phase_change(old, new, spec, name, namespace, patch, **kwargs):
    """
    Drives the state machine: Ready → Running → Sleeping → Running → ... → Completed
    Called whenever status.phase changes.
    """
    if new == 'Ready':
        start_next_run(spec, name, namespace, patch)

@kopf.on.event('', 'v1', 'pods',
               labels={'sherlock.io/suite-name': kopf.PRESENT})
def on_pod_event(event, name, namespace, patch, **kwargs):
    """
    Watches Job pods for completion/failure.
    On completion: collect artifacts, parse results, advance to next run.
    On failure: apply failurePolicy (abort/retry/continue).
    """
    ...
```

The `expand_cartesian` utility in `operator/matrix.py` is shared — it takes
the sweep dict and returns a list of parameter dicts, one per combination.
Your handler does not need to implement this.

Key labels to set on all Jobs and pods created by your handler:
```python
labels = {
    'sherlock.io/suite-name': name,
    'sherlock.io/suite-kind': 'Sherlock<Name>Suite',
    'sherlock.io/run-name': run_name,
}
```
These labels are how the operator finds pods to watch and how artifacts are
associated with the right run.

---

## Step 6 — Python result parser

Create `operator/parsers/<name>.py`.

The parser has one required function:

```python
def parse(raw_output: str, params: dict) -> dict:
    """
    Parse raw benchmark output (stdout log from the container) into a
    structured result dict matching the CRD status.runMatrix[].result schema.

    Args:
        raw_output: full stdout from the benchmark container pod log
        params: the resolved parameter dict for this run (from runMatrix[].params)

    Returns:
        dict with keys matching status.runMatrix[].result fields.
        Always include the primary metric. Leave unknown fields absent
        rather than setting them to 0 — the operator handles missing fields.
    """
    ...
    return {
        'tps': ...,             # your benchmark's primary metric key
        'latencyAvgMs': ...,
        'latencyP95Ms': ...,
    }
```

And one optional function for stat-collector output (same across all parsers,
but you can override if needed):

```python
def parse_storage_stats(iostat_output: str, vmstat_output: str) -> dict:
    """
    Parse iostat/vmstat output from the stat collector pod into storage metrics.
    The default implementation in operator/parsers/base.py handles this for
    most cases — only override if your storage plugin produces different output.
    """
    from operator.parsers.base import parse_storage_stats as default
    return default(iostat_output, vmstat_output)
```

Parser tips:
- fio with `--output-format=json`: use `json.loads()`, extract
  `jobs[0]['read']['iops']`, `jobs[0]['read']['lat_ns']['percentile']`, etc.
- pgbench: regex for `tps = \d+\.\d+` in the last line of output
- sysbench: regex for `transactions: \d+ \(\d+\.\d+ per sec\)`
- hammerdb: parse the `NOPM` value from the HammerDB result line
- Always strip ANSI escape codes before parsing: `re.sub(r'\x1b\[[0-9;]*m', '', raw)`

---

## Step 7 — Register the new CRD in the operator

Add your new kind to `operator/main.py`:

```python
# operator/main.py
import operator.handlers.hammerdb
import operator.handlers.pgbench
import operator.handlers.sysbench
import operator.handlers.ycsb
import operator.handlers.fio
import operator.handlers.<name>    # ← add this line
```

kopf auto-discovers handlers via the decorators, but the module must be imported
for the decorators to run.

---

## Step 8 — Update the CRD README

Add your new benchmark to the table in `operator/crds/README.md`:

```markdown
| `Sherlock<Name>Suite` | <benchmark tool> | <database/storage> |
```

And add its primary metric to the metrics table.

---

## Checklist

Before opening a PR:

- [ ] `operator/crds/benchmarks/<name>-suite.yaml` — CRD schema
- [ ] `operator/crds/examples/<name>-example.yaml` — at least one working example
- [ ] `containers/<name>_container/Dockerfile` — container image definition
- [ ] `containers/<name>_container/runs/run_<name>` — benchmark entrypoint
- [ ] `operator/handlers/<name>.py` — kopf handler
- [ ] `operator/parsers/<name>.py` — result parser
- [ ] `operator/main.py` updated to import new handler
- [ ] `operator/crds/README.md` updated with new benchmark
- [ ] Container image built and pushed to `quay.io/sagyvolkov/<name>_container:<version>`
- [ ] `kubectl apply -f operator/crds/benchmarks/<name>-suite.yaml` succeeds
- [ ] `kubectl apply -f operator/crds/examples/<name>-example.yaml` succeeds
- [ ] At least one successful end-to-end test run with artifacts collected

---

## A note on database vs. storage-only benchmarks

The main structural difference in the CRD:

| | DB benchmark | Storage-only (fio) |
|--|--|--|
| Top-level infra section | `spec.database` | `spec.storage` |
| Infrastructure phase | DB pod scheduling + readiness | PVC provisioning only |
| `conditions[].type` | `DatabaseReady` | `PVCsReady` |
| Container image purpose | connect to DB, run queries | write directly to PVC |
| Two-phase setup | yes (init schema + load data, then benchmark) | no (fio writes its own data) |

If your benchmark connects to a database that Sherlock already deploys (PostgreSQL,
MySQL, MongoDB, MSSQL), reuse the existing DB deployment logic in the operator —
don't re-implement it. The handler for your benchmark should call the shared
`operator.db.deploy(db_type, spec)` function and wait for `DatabaseReady` before
starting benchmark jobs.
