# Sherlock v2 — CRD Schemas

This directory contains the CustomResourceDefinition schemas for Sherlock v2.

Sherlock v2 replaces the original bash-based orchestration with a Kubernetes operator.
Each benchmark gets its own Suite CRD with a typed parameter sweep model that mirrors
the nested for-loop pattern from the original shell scripts.

## Directory structure

```
operator/crds/
  base/
    common-types.yaml         # Shared field documentation (DatabaseSpec, ExecutionSpec,
                              # ArtifactSpec) — not a standalone CRD
  benchmarks/
    hammerdb-suite.yaml       # SherlockHammerDBSuite — MSSQL / MySQL via HammerDB TPC-C
    pgbench-suite.yaml        # SherlockPgbenchSuite  — PostgreSQL via pgbench
    sysbench-suite.yaml       # SherlockSysbenchSuite — MySQL / PostgreSQL via sysbench
    ycsb-suite.yaml           # SherlockYCSBSuite     — MongoDB via YCSB
  examples/
    hammerdb-example.yaml     # 6-run VU sweep, PVC artifact destination
    pgbench-example.yaml      # 12-run rw-ratio sweep, S3 artifact destination
    sysbench-example.yaml     # 8-run thread sweep, PVC + retryN failure policy
    ycsb-example.yaml         # 9-run workload sweep, local pull destination
```

## How the parameter sweep works

Every Suite CRD has a `spec.sweep` section where each field takes a list of values.
The operator computes the **cartesian product** of all multi-value fields, generating
one run per combination. This replaces the nested `for` loops in the original scripts.

```yaml
# HammerDB example: 3 × 2 = 6 runs
sweep:
  virtualUsers: [8, 16, 32]   # sweep axis
  warehouses:   [100]         # fixed param
  rampupMins:   [2]           # fixed param
  testMins:     [5, 10]       # sweep axis
```

Run names are auto-generated from axis values:
`mssql-vu-sweep-vu8-wh100-rm2-tm5`, `mssql-vu-sweep-vu8-wh100-rm2-tm10`, ...

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

The `spec.database.storagePlugin` field controls storage-side metric collection:

| plugin    | How metrics are collected                          |
|-----------|----------------------------------------------------|
| generic   | iostat/vmstat on nodes where DB pods run (default) |
| lightbits | lbcli + generic                                    |
| odf       | ceph CLI + generic                                 |
| portworx  | pxctl + generic                                    |

## Phase state machine

```
Pending → Deploying → Ready → Running ⇄ Sleeping → Collecting → Completed
                                  ↓                                  ↓
                                Failed                           Degraded
```

## Benchmark-specific primary metrics

| Benchmark | Primary metric       | Storage metrics (all benchmarks)     |
|-----------|----------------------|--------------------------------------|
| HammerDB  | nopm, tpm            | avgReadIops, avgWriteIops,           |
| pgbench   | tps                  | avgReadBwMBs, avgWriteBwMBs,         |
| sysbench  | tps, qps             | avgLatencyMs, p95LatencyMs,          |
| YCSB      | throughputOpsPerSec  | p99LatencyMs                         |

## Future / reserved fields

- `spec.database.partitionedTables` (pgbench) — PostgreSQL partitioned table support via CNPG
- `spec.database.partitionMethod` (pgbench) — range | hash
- Prometheus/Grafana observability integration (Phase 2)
- Full storage plugin implementations beyond `generic`
