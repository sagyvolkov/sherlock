# Sherlock v2 — CRD Schemas

This directory contains the CustomResourceDefinition schemas for Sherlock v2.

Sherlock v2 replaces the original bash-based orchestration with a Kubernetes operator.
Each benchmark gets its own Suite CRD with a typed parameter sweep model that mirrors
the nested for-loop pattern from the original shell scripts.

## Directory structure

```
operator/crds/
  base/
    common-types.yaml             # Shared field documentation (not a standalone CRD)
  benchmarks/
    fio-suite.yaml                # SherlockFioSuite      — fio (any PVC, block or filesystem)
    hammerdb-suite.yaml           # SherlockHammerDBSuite — MSSQL / MySQL via HammerDB TPC-C
    pgbench-suite.yaml            # SherlockPgbenchSuite  — PostgreSQL via pgbench
    sysbench-suite.yaml           # SherlockSysbenchSuite — MySQL / PostgreSQL via sysbench
    ycsb-suite.yaml               # SherlockYCSBSuite     — MongoDB via YCSB
  examples/
    fio-example.yaml              # Block mode NVMe-oF sweep + filesystem throughput sweep
    hammerdb-example.yaml         # 6-run VU sweep, PVC artifact destination
    pgbench-example.yaml          # 12-run rw-ratio sweep, S3 artifact destination
    sysbench-example.yaml         # 8-run thread sweep, PVC + retryN failure policy
    ycsb-example.yaml             # 9-run workload sweep, local pull destination
  README.md                       # This file
  ADDING_A_BENCHMARK.md           # Guide for contributing new benchmarks
```

## Benchmarks

| CRD | Kind | Benchmark tool | Target |
|-----|------|----------------|--------|
| `fio-suite.yaml` | `SherlockFioSuite` | fio | Any PVC (block or filesystem) |
| `hammerdb-suite.yaml` | `SherlockHammerDBSuite` | HammerDB TPC-C | MSSQL, MySQL |
| `pgbench-suite.yaml` | `SherlockPgbenchSuite` | pgbench | PostgreSQL |
| `sysbench-suite.yaml` | `SherlockSysbenchSuite` | sysbench | MySQL, PostgreSQL |
| `ycsb-suite.yaml` | `SherlockYCSBSuite` | YCSB | MongoDB |

> **fio is the only benchmark without a database layer.** It writes directly to PVCs
> and is the best tool for raw storage characterization before introducing a database.
> Run fio first to establish a storage baseline, then run the DB benchmarks on the
> same storage class to understand the database overhead.

## How the parameter sweep works

Every Suite CRD has a `spec.sweep` section where each field takes a list of values.
The operator computes the **cartesian product** of all multi-value fields, generating
one run per combination. This replaces the nested `for` loops in the original scripts.

```yaml
# HammerDB example: 3 × 2 = 6 runs
sweep:
  virtualUsers: [8, 16, 32]   # sweep axis (3 values)
  warehouses:   [100]         # fixed param (1 value)
  rampupMins:   [2]           # fixed param (1 value)
  testMins:     [5, 10]       # sweep axis (2 values)
```

```yaml
# fio example: 5 × 3 = 15 runs
sweep:
  blockSize:   ["4k", "8k", "64k", "128k", "1m"]   # sweep axis (5 values)
  rwMixWrite:  [0, 30, 100]                          # sweep axis (3 values)
  ioPattern:   [randrw]                              # fixed param (1 value)
  runtime:     [60]                                  # fixed param (1 value)
```

Run names are auto-generated from axis values:
`nvme-bs-sweep-bs4k-rw0-randrw-rt60-j4-qd4-2048g`

## Artifact destinations

| type  | Where data goes              | Best for                           |
|-------|------------------------------|------------------------------------|
| pvc   | PVC on the cluster           | In-cluster access, CI pipelines    |
| s3    | S3-compatible object storage | Long-term archiving, multi-cluster |
| local | emptyDir, user pulls it      | Laptop/workstation, quick tests    |

```bash
# Pull artifacts locally after a run
kubectl sherlock pull <suite-name> ./results/
```

## Storage plugins

The `spec.database.storagePlugin` (or `spec.storage.storagePlugin` for fio) field
controls storage-side metric collection alongside the benchmark:

| plugin    | How metrics are collected                          |
|-----------|----------------------------------------------------|
| generic   | iostat/vmstat on nodes where pods run (default)    |
| lightbits | lbcli stats + generic                              |
| odf       | ceph CLI stats + generic                           |
| portworx  | pxctl stats + generic                              |

## Failure policies

| policy | Behaviour |
|--------|-----------|
| `abortAll` | Cancel all remaining runs on first failure |
| `retryN` | Retry failed run up to `retryLimit` times, then abort |
| `continueAndReport` | Continue remaining runs, mark suite `Degraded` at end |

## Phase state machine

```
Pending → Deploying → Ready → Running ⇄ Sleeping → Collecting → Completed
                                  ↓                                  ↓
                                Failed                           Degraded
```

fio adds a `Provisioning` phase between `Pending` and `Ready` to account for
large block PVC provisioning time.

## Benchmark-specific primary metrics

| Benchmark | Primary metric(s)    | Latency unit | Storage metrics (all) |
|-----------|----------------------|--------------|----------------------|
| fio       | readIops, writeIops, readBwMBs, writeBwMBs | microseconds (from fio JSON) | avgReadIops, avgWriteIops, avgReadBwMBs, avgWriteBwMBs, avgLatencyMs, p95LatencyMs, p99LatencyMs |
| HammerDB  | nopm, tpm            | —            | (same) |
| pgbench   | tps                  | milliseconds | (same) |
| sysbench  | tps, qps             | milliseconds | (same) |
| YCSB      | throughputOpsPerSec  | microseconds | (same) |

The shared storage metrics (rightmost column) come from stat collector pods running
iostat/vmstat on the same nodes, enabling cross-suite storage comparison.

## Adding a new benchmark

See [ADDING_A_BENCHMARK.md](./ADDING_A_BENCHMARK.md) for a complete guide covering:
- CRD schema conventions
- Container image structure
- kopf operator handler pattern
- Python result parser interface
- End-to-end checklist

## Future / reserved fields

- `spec.database.partitionedTables` (pgbench) — PostgreSQL partitioned table support
- `spec.database.partitionMethod` (pgbench) — range | hash partition strategy
- Prometheus/Grafana observability integration (Phase 2 observability)
- Full storage plugin implementations beyond `generic`
