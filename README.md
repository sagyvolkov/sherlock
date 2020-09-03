# Sherlock - the old skool detective for database performance on OCP

Like the great detective, the idea behind the set of scripts in this repo is to investigate performance of Software Define Storage (SDS) in the Openshift/Kuberentes domain using generic database workloads.
Old skool is the moto here, hence why it was written in bash (run anywhere), using old "fashioned" performance measurement tools (iostat, mpstat, vmstat and so on) and without any web interface and cool graphics. Pretty much Commodore Amiga style :)

The scripts will help you setup the database on your OCP/k8s cluster, making sure the databases are spread equally across your worker nodes, populate data and then run the tests.

It is optional, but you can choose to collect statistics from the nodes running the databases and the nodes running the SDS (can be the same nodes) so you can look at what happened at the OS level when the workload ran.

This project was created to measure database performance using  Openshift Container Storage (OCS), which is Ceph based, but can easily run using any other SDS provider for OCP/k8s (in fact it was tested using other SDS and cloud storage providers - the only hardcoded notion for OCS/Ceph is the optional function to measure RBD based PVCs performance).

## Requirements

The only real requirement is bash > 4.0 
(for macOS, just "brew install bash", then either use "/usr/local/bin/bash" in the scripts or update /etc/shells). 

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

### List of files/scripts in the
_create_databases_ - Create databases based on parameters from the config file. the script will make sure to spread the database equally on nodes that are part of the WORKERS_LIST_FILE variable.

_run_database_workload-parallel_ - Will run the jobs of pods that connects to the database and create/run on the data.

_run_loops_ - just a simple script to run _run_database_workload-parallel_ in a loop and display the stats from the runs.

_print_sysbench_results_ - Use this script to display the results of _run_database_workload-parallel_ in a csv, table or just simple text.

_sherlock.config_ - a sample config file (see section below).

### fio
The Flexible I/O (fio) tester is a well known artificial workload generator with many options to test storage devices.
While I prefer to test with real-life workloads, fio is a very fast tool to measure performance of SDS, so I've created a very small pod that just runs fio and it can help with SDS assessment.
The run_fio_job script is pretty self explanatory. The run_fio_tests script is a sample script when you want to run multiple options in a serial fashion and then examine the results.

### The sherlock.config file
Most variables are self explanatory, however, if you are not aware of sysbench of pgbench, it might get confusing:

OUTPUT_INTERVAL - how often the workload will output data, it will impact the size of the logs and even the performance (for example, if you run output every second while heavily stress the cluster).

SYSBENCH_NUMBER_OF_TABLES & SYSBENCH_ROWS_IN_TABLE - numbers of tables in the sysbench schema and number of rows to create in each table, basically impact the size of the database. (for example, 400 tables with 1,000,000 rows in each table is roughly 100GB of database size).

SYSBENCH_NUMBER_OF_INSERTS/UPDATES/NON_INDEX_UPDATES - using these variables you can impact the ratio of read/write IOs that will be performed on the SDS via manipulating the DB transactions. (in sysbench itself these corresponds to delete_inserts, index_updates and non_index_updates). a value of 1 in all 3 variables will have roughly a 70% read/30% write IOPS ratio.

PGBENCH_VACUUM - whether to perform a vacuum (sort of a garbage collection and analyzing the database) after initially injecting all the data to the database (see https://www.postgresql.org/docs/11/sql-vacuum.html)

KUBE_CMD - what binary to use for all instructions to the k8s cluster, either the oc for Openshift of kubectl for Kubernetes.

NUMBER_OF_WORKERS & DB_PER_WORKER - decide the spread of database in the cluster.

WORKERS_LIST_FILE - list of the nodes in the cluster you want to deploy databases. (Can be identical to SDS_LIST_FILE in a converge cluster)

SDS_LIST_FILE - list of the nodes in the cluster that run the SDS. (Can be identical to WORKERS_LIST_FILE in a converge cluster)

PROJECT_NAME - just a project name to create all the database and run all the tests in it.

DB_TYPE - pgsql for PostgreSQL or mysql for MySQL.

STORAGE_CLASS - the SDS storage class you want to use to create the PVCs for the databases. (for example, ocs-storagecluster-ceph-rbd for OCS RBD, or gp2 for AWS gp2 volumes).

STATS - if set to true, every run will also deploy a tiny stats pod in each node. Depending on the type of node (worker or SDS or both) it will collect OS based stats from vmstat, mpstat, iostat and ifconfig.

STATS_INTERVAL - how often to collect stats.

SDS_DEVICES - list of the sds devices (separated by space) that are used for the SDS (right now, it has to be the same device name on each node, and in most installed, this will be the case anyhow).

SDS_NETWORK_INTERFACES - list of the sds network interface/s (separated by space) that are used by the SDS. like SDS_DEVICES, have to be identical for all nodes.

RBD_STATS - for Ceph based SDSs (OCS or Rook/Ceph), you can show IO stats per each rbd devices/volume per database, this is the actual PVC that each database is using.

## Security note:
You'll notice in the _create_database_ and _run_database_workload-parallel_ scripts a few scc modifications. 
Security was/is never my concern when writing these scripts, but feel free to change or suggest changes to the scripts to make them more secure.

## Workloads status

Sysbench: works on MySQL and PostgreSQL.

Pgbench: works on PostgreSQL (TCP-B mostly writes by default)

YCSB: works on MongoDB (In Development)

Hammerdb: works on SQL Server (In Development)

### ToDo:
1. Adding SQLserver using hammerdb
2. Adding PCP (Performance Co-Pilot) as a stats pods vs plain sysstat data. You will be able to choose between type of stats collection, but the PCP stats are going to be a lot more comprehensive and includes also graphs.
3. Adding MongoDB using YCSB
4. Adding redis using YCSB
5. Move to Python or at least have a version in python.
6. Maybe operatorize the scripts :)
