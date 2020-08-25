# Sherlock - the old skool detective for database performance on OCP

Like the great detective, the idea behind the set of scripts in this repo is to investigate performance of Software Define Storage (SDS) in the Openshift/Kuberentes domain using generic database workloads.
Old skool is the moto here, hence why it was written in bash (run anywhere), using old "fashioned" performance measurment tools (iostat, mpstat, vmstat and so on) and without any web interface and cool graphics. Pretty much Commodore Amiga style :)

The scripts will help you setup the database on your OCP/k8s cluster, making sure the databases are spread equally across your worker nodes, populate data and then run the tests.

It is optional, but you can choose to collect statistics from the nodes running the databases and the nodes running the SDS (can be the same nodes) so you can look at what happened at the OS level when the workload ran.

This project was created to measure database peformance using  Openshift Container Storage (OCS), which is Ceph based, but can easily run using any other SDS provider for OCP/k8s (in fact it was tested using other SDS and cloud storage providers - the only hardcoded notion for OCS/Ceph is the optional function to measure RBD based PVCs performance).

## Requirements
Bash > 4.0 (for macOS, just "brew install bash", then either use "/usr/local/bin/bash" in the scripts or update /etc/shells).

### Deployments

Nothing to actually deploy. just grab the script from the repo.

### Database creation
```bash
create_databases
```
all the settings are in the sherlock.config file.

### Preparing data
#### Sysbench:
```bash
run_database_workload-parallel -b sysbench -j prepare -c <path to config>
```
#### Pgbench
```bash
run_database_workload-parallel -b pgbench -j prepare -c <path to config>
```

### Running a workload
#### Sysbench:
```bash
run_database_workload-parallel -b sysbench -j run -c <path to config> -n <some name for the run>
```
#### Pgbench
```bash
run_database_workload-parallel -b pgbench -j run -c <path to config> -n <some name for the run>
```


The script are using containers that are design specifically to run database benchmarks.

## Workloads status

sysbench: works on MySQL and PostgreSQL.

pgbench: works on PostgreSQL (TCP-B mostly writes by default)

YCSB: works on MongoDB

hammerdb: works on SQL Server (In Development)
