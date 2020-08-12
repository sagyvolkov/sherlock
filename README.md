# sherlock

Like the great detective, the idea behind the set of scripts in this repo is to investigate performance of Software Define Storage (SDS) using generic database workloads.

The scripts are meant to be used with OpenShift/Kubernetes.

The script are using containers that are design specifically to run database benchmarks.

## Workloads status

sysbench: works on MySQL and PostgreSQL.

pgbench: works on PostgreSQL (TCP-B mostly writes by default)

YCSB: works on MongoDB

hammerdb: works on SQL Server (In Development)
