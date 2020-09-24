# Sherlock - the old skool detective for database performance on OCP

<pre>
   ,_       
 ,'  `\,_   
 |_,-'_)    
 /##c '\  ( 
' |'  -{.  )
  /\__-' \[]
 /`-_`\     
 '     \    
</pre>

Like the great detective, the idea behind the set of scripts in this repo is to investigate performance of Software Define Storage (SDS) in the Openshift/Kuberentes domain using generic database workloads.
Old skool is the moto here, hence why it was written in bash (run anywhere), using old "fashioned" performance measurement tools (iostat, mpstat, vmstat and so on) and without any web interface and cool graphics. Pretty much Commodore Amiga style :)

The scripts will help you setup the database on your OCP/k8s cluster, making sure the databases are spread equally across your worker nodes, populate data and then run the tests.

It is optional, but you can choose to collect statistics from the nodes running the databases and the nodes running the SDS (can be the same nodes) so you can look at what happened at the OS level when the workload ran.

This project was created to measure database performance using  Openshift Container Storage (OCS), which is Ceph based, but can easily run using any other SDS provider for OCP/k8s (in fact it was tested using other SDS and cloud storage providers - the only hardcoded notion for OCS/Ceph is the optional function to measure RBD based PVCs performance).

## Requirements

The only real requirement is bash > 4.0 (associative arrays are heavily used here).
(for macOS, just "brew install bash", then either use "#!/usr/local/bin/bash" instead of "#!/bin/bash" in the scripts or update /etc/shells). 

### Deployments

Nothing to actually deploy. just grab the scripts from the repo.

### Database creation
Once you figured the number of databases per node, the pod resources and the size of each database and configure it in the sherlock.conf file, all you need to do is run:
```bash
create_databases
```
### Preparing data
#### Sysbench:
```bash
run_database_workload-parallel -b sysbench -j prepare -c <path to config>
```
#### Pgbench
```bash
run_database_workload-parallel -b pgbench -j prepare -c <path to config>
```
#### HammerDB
```bash
run_database_workload-parallel -b hammerdb -j prepare -c <path to config>
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
#### HammerDB
```bash
run_database_workload-parallel -b hammerdb -j run -c <path to config> -n <some name for the run>
```

### List of files/scripts in the
_create_databases_ - Create databases based on parameters from the config file. the script will make sure to spread the database equally on nodes that are part of the WORKERS_LIST_FILE variable.

_run_database_workload-parallel_ - Will run the jobs of pods that connects to the database and create/run on the data.

_run_loops_ - just a simple script to run _run_database_workload-parallel_ in a loop and display the stats from the runs.

_print_results_ - Use this script to display the results of _run_database_workload-parallel_ in a csv, table or just simple text. The script also accept a parameter whether it is used to display results for a single run or for a "loop" of runs created via the _run_loops_ script

_sherlock.config_ - a the sherlock config file. You can rename the config file, just make sure to provide the correct name and path when you run sherlock.

### fio
The Flexible I/O (fio) tester is a well known artificial workload generator with many options to test storage devices.
While I prefer to test with real-life workloads, fio is a very fast tool to measure performance of SDS, so I've created a very small pod that just runs fio and it can help with SDS assessment.
The run_fio_job script is pretty self explanatory. The run_fio_tests script is a sample script when you want to run multiple options in a serial fashion and then examine the results.

### The sherlock.config file
Most variables are self explanatory and are explained in the sherlock.config file

## Security note:
You'll notice in the _create_database_ and _run_database_workload-parallel_ scripts a few scc modifications. 
Security was/is never my concern when writing these scripts, but feel free to change or suggest changes to the scripts to make them more secure.

## Workloads and database status:

Sysbench: works on MySQL and PostgreSQL.

Pgbench: works on PostgreSQL (TCP-B mostly writes by default)

Hammerdb: works on SQL Server

YCSB: works on MongoDB (In Development)

## Output examples:
The following outputs are based on PostgreSQL, 3 nodes, 2 database per node:
## create_databases:
```bash
$ ./create_databases 
Now using project "postgresql" on server "https://api.vaelin.ocsonazure.com:6443".

You can add applications to this project with the 'new-app' command. For example, try:

    oc new-app ruby~https://github.com/sclorg/ruby-ex.git

to build a new example application in Ruby. Or use kubectl to deploy a simple Kubernetes application:

    kubectl create deployment hello-node --image=gcr.io/hello-minikube-zero-install/hello-node

20200924:04:12:45: Creating postgresql database pod postgresql-0 on node vaelin-nkmld-worker-eastus21-bwr2l
persistentvolumeclaim/postgresql-pvc-0 created
deployment.apps/postgresql-0 created
service/postgresql-0 created
pod/postgresql-0-6cd6667847-r9hlw condition met
20200924:04:13:13: Creating postgresql database pod postgresql-1 on node vaelin-nkmld-worker-eastus21-jt7qb
persistentvolumeclaim/postgresql-pvc-1 created
deployment.apps/postgresql-1 created
service/postgresql-1 created
pod/postgresql-1-64bbb6f567-csvfn condition met
20200924:04:13:44: Creating postgresql database pod postgresql-2 on node vaelin-nkmld-worker-eastus21-tvflp
persistentvolumeclaim/postgresql-pvc-2 created
deployment.apps/postgresql-2 created
service/postgresql-2 created
pod/postgresql-2-75db9d8688-btgg2 condition met
20200924:04:14:17: Creating postgresql database pod postgresql-3 on node vaelin-nkmld-worker-eastus21-bwr2l
persistentvolumeclaim/postgresql-pvc-3 created
deployment.apps/postgresql-3 created
service/postgresql-3 created
pod/postgresql-3-7846f9476-786ns condition met
20200924:04:14:27: Creating postgresql database pod postgresql-4 on node vaelin-nkmld-worker-eastus21-jt7qb
persistentvolumeclaim/postgresql-pvc-4 created
deployment.apps/postgresql-4 created
service/postgresql-4 created
pod/postgresql-4-8557948b7d-pd9ht condition met
20200924:04:14:43: Creating postgresql database pod postgresql-5 on node vaelin-nkmld-worker-eastus21-tvflp
persistentvolumeclaim/postgresql-pvc-5 created
deployment.apps/postgresql-5 created
service/postgresql-5 created
pod/postgresql-5-6bdf5d8b8c-zg762 condition met
```
## run_database_workload-parallel preparing the database:
```bash
$ ./run_database_workload-parallel -b sysbench -j prepare -c sherlock.config 
20200924:05:01:30: Starting sysbench job for prepare in deployment postgresql-0 with database ip 10.129.2.12 ...
20200924:05:01:31: job.batch/sysbench-prepare-postgresql-0-maccnb-494r6 is using sysbench pod sysbench-prepare-postgresql-0-maccnb-494r6-tsv2d on node vaelin-nkmld-worker-eastus21-bwr2l
20200924:05:01:31: Starting sysbench job for prepare in deployment postgresql-1 with database ip 10.128.2.12 ...
20200924:05:01:32: job.batch/sysbench-prepare-postgresql-1-maccnb-xfrr2 is using sysbench pod sysbench-prepare-postgresql-1-maccnb-xfrr2-fr7v9 on node vaelin-nkmld-worker-eastus21-jt7qb
20200924:05:01:32: Starting sysbench job for prepare in deployment postgresql-2 with database ip 10.131.0.23 ...
20200924:05:01:32: job.batch/sysbench-prepare-postgresql-2-maccnb-td6cr is using sysbench pod sysbench-prepare-postgresql-2-maccnb-td6cr-g78kj on node vaelin-nkmld-worker-eastus21-tvflp
20200924:05:01:33: Starting sysbench job for prepare in deployment postgresql-3 with database ip 10.129.2.13 ...
20200924:05:01:33: job.batch/sysbench-prepare-postgresql-3-maccnb-gqrc4 is using sysbench pod sysbench-prepare-postgresql-3-maccnb-gqrc4-92xxn on node vaelin-nkmld-worker-eastus21-bwr2l
20200924:05:01:33: Starting sysbench job for prepare in deployment postgresql-4 with database ip 10.128.2.13 ...
20200924:05:01:34: job.batch/sysbench-prepare-postgresql-4-maccnb-wb4d8 is using sysbench pod sysbench-prepare-postgresql-4-maccnb-wb4d8-4gz2h on node vaelin-nkmld-worker-eastus21-jt7qb
20200924:05:01:34: Starting sysbench job for prepare in deployment postgresql-5 with database ip 10.131.0.24 ...
20200924:05:01:35: job.batch/sysbench-prepare-postgresql-5-maccnb-xscr4 is using sysbench pod sysbench-prepare-postgresql-5-maccnb-xscr4-z79fb on node vaelin-nkmld-worker-eastus21-tvflp
```
as you can see, each database gets its own job that consists of a pod running sysbench to prepare (create and insert) the data, you can monitor the progress by looking at the pods logs or just monitoring for the jobs to finish.
## run_database_workload-parallel running sysbench and collecting stats:
```bash
$ ./run_database_workload-parallel -b sysbench -j run -c sherlock.config -n psql1
clusterrole.rbac.authorization.k8s.io/system:openshift:scc:hostnetwork added: "default"
20200924:06:04:52: Starting to collect stats on worker node vaelin-nkmld-worker-eastus21-bwr2l...
20200924:06:04:53: Starting to collect stats on worker node vaelin-nkmld-worker-eastus21-jt7qb...
20200924:06:04:54: Starting to collect stats on worker node vaelin-nkmld-worker-eastus21-tvflp...
20200924:06:04:54: Starting to collect stats on sds node vaelin-nkmld-cephnode-eastus21-5c7xj...
20200924:06:04:55: Starting to collect stats on sds node vaelin-nkmld-cephnode-eastus21-5fv74...
20200924:06:04:56: Starting to collect stats on sds node vaelin-nkmld-cephnode-eastus21-8fwqv...
20200924:06:04:56: Starting sysbench job for run in deployment postgresql-0 with database ip 10.129.2.12 ...
20200924:06:04:57: job.batch/sysbench-run-postgresql-0-psql1-9ccs5 is using sysbench pod sysbench-run-postgresql-0-psql1-9ccs5-2krw5 on node vaelin-nkmld-worker-eastus21-bwr2l
20200924:06:04:57: Starting sysbench job for run in deployment postgresql-1 with database ip 10.128.2.12 ...
20200924:06:04:58: job.batch/sysbench-run-postgresql-1-psql1-c6r6k is using sysbench pod sysbench-run-postgresql-1-psql1-c6r6k-q85sg on node vaelin-nkmld-worker-eastus21-jt7qb
20200924:06:04:58: Starting sysbench job for run in deployment postgresql-2 with database ip 10.131.0.23 ...
20200924:06:04:58: job.batch/sysbench-run-postgresql-2-psql1-nsggv is using sysbench pod sysbench-run-postgresql-2-psql1-nsggv-xcmrs on node vaelin-nkmld-worker-eastus21-tvflp
20200924:06:04:59: Starting sysbench job for run in deployment postgresql-3 with database ip 10.129.2.13 ...
20200924:06:04:59: job.batch/sysbench-run-postgresql-3-psql1-tg8cn is using sysbench pod sysbench-run-postgresql-3-psql1-tg8cn-cqz56 on node vaelin-nkmld-worker-eastus21-bwr2l
20200924:06:04:59: Starting sysbench job for run in deployment postgresql-4 with database ip 10.128.2.13 ...
20200924:06:05:00: job.batch/sysbench-run-postgresql-4-psql1-nnzwt is using sysbench pod sysbench-run-postgresql-4-psql1-nnzwt-ktscv on node vaelin-nkmld-worker-eastus21-jt7qb
20200924:06:05:00: Starting sysbench job for run in deployment postgresql-5 with database ip 10.131.0.24 ...
20200924:06:05:01: job.batch/sysbench-run-postgresql-5-psql1-bvd8c is using sysbench pod sysbench-run-postgresql-5-psql1-bvd8c-kl89z on node vaelin-nkmld-worker-eastus21-tvflp
20200924:06:05:01: Waiting for jobs to complete ...
job.batch/sysbench-run-postgresql-3-psql1-tg8cn condition met
job.batch/stats-sds-psql1-vaelin-nkmld-cephnode-eastus21-8fwqv-gg89r condition met
job.batch/sysbench-run-postgresql-4-psql1-nnzwt condition met
job.batch/sysbench-run-postgresql-2-psql1-nsggv condition met
job.batch/stats-sds-psql1-vaelin-nkmld-cephnode-eastus21-5c7xj-fmdj2 condition met
job.batch/sysbench-run-postgresql-0-psql1-9ccs5 condition met
job.batch/stats-worker-psql1-vaelin-nkmld-worker-eastus21-bwr2l-fzqqt condition met
job.batch/stats-sds-psql1-vaelin-nkmld-cephnode-eastus21-5fv74-7fbql condition met
job.batch/stats-worker-psql1-vaelin-nkmld-worker-eastus21-jt7qb-s4trc condition met
job.batch/stats-worker-psql1-vaelin-nkmld-worker-eastus21-tvflp-5f58g condition met
job.batch/sysbench-run-postgresql-1-psql1-c6r6k condition met
job.batch/sysbench-run-postgresql-5-psql1-bvd8c condition met
20200924:06:10:30: All jobs are done, getting logs ...
20200924:06:10:32: End of ./run_database_workload-parallel script ...
```
as you can see, in the run mode, the script will wait for all jobs to continue.<br/>
We have 6 jobs running sysbench, 3 jobs collecting stats on the worker nodes running the databases and 3 jobs collecting stats on the worker nodes running the SDS, so total of 12 jobs.<br/>
Once all are finished the script will collect the logs from all jobs and save them in the directory named "psql1" (the name we chose for our run example).<br/>
(because the _run_database_workload-parallel_ script will wait for all jobs to finish when you use "-j run", it is recommended to run the script from a program that will keep your session active like "screen" or "tmux").

Inside the psql1 directory:
```bash
[sagy@bastion psql1]$ ls -l
total 868
-rw-rw-r--. 1 sagy sagy   5452 Sep 24 06:05 sherlock.config
-rw-rw-r--. 1 sagy sagy 146512 Sep 24 06:10 stats-sds-psql1-vaelin-nkmld-cephnode-eastus21-5c7xj-fmdj2kswrt.log
-rw-rw-r--. 1 sagy sagy 147570 Sep 24 06:10 stats-sds-psql1-vaelin-nkmld-cephnode-eastus21-5fv74-7fbqlz8f9b.log
-rw-rw-r--. 1 sagy sagy 153973 Sep 24 06:10 stats-sds-psql1-vaelin-nkmld-cephnode-eastus21-8fwqv-gg89r8h5tm.log
-rw-rw-r--. 1 sagy sagy 118620 Sep 24 06:10 stats-worker-psql1-vaelin-nkmld-worker-eastus21-bwr2l-fzqqq5bfv.log
-rw-rw-r--. 1 sagy sagy 121996 Sep 24 06:10 stats-worker-psql1-vaelin-nkmld-worker-eastus21-jt7qb-s4trbgwzt.log
-rw-rw-r--. 1 sagy sagy 132895 Sep 24 06:10 stats-worker-psql1-vaelin-nkmld-worker-eastus21-tvflp-5f58w527v.log
-rw-rw-r--. 1 sagy sagy   5192 Sep 24 06:10 sysbench-run-postgresql-0-psql1-9ccs5-2krw5.log
-rw-rw-r--. 1 sagy sagy   5197 Sep 24 06:10 sysbench-run-postgresql-1-psql1-c6r6k-q85sg.log
-rw-rw-r--. 1 sagy sagy   5194 Sep 24 06:10 sysbench-run-postgresql-2-psql1-nsggv-xcmrs.log
-rw-rw-r--. 1 sagy sagy   5189 Sep 24 06:10 sysbench-run-postgresql-3-psql1-tg8cn-cqz56.log
-rw-rw-r--. 1 sagy sagy   5193 Sep 24 06:10 sysbench-run-postgresql-4-psql1-nnzwt-ktscv.log
-rw-rw-r--. 1 sagy sagy   5196 Sep 24 06:10 sysbench-run-postgresql-5-psql1-bvd8c-kl89z.log
```
The psql1 (the name in our example for the run) holds the following files:
- logs from the jobs/pods running sysbench (file name that start with the benchmark we used, in our case sysbench)
- logs from the jobs/pods running stats from the nodes running the databases (file name that start with stats-worker)
- logs from the jobs/pods running stats from the nodes the SDS (file name start with stats-sds)
- a copy of the sherlock config file used for this run. This way you can go back and understand how many nodes used, how many databases, size of databases, pod resources and so on.

You can use the _print_results_ script to print the sysbench results:
```bash
$ ./print_results psql1 single simple sysbench sherlock.config 
Run name: psql1
Average TPS for all databases: 264.56
Average latency for all databases: 22.70
Average 95% latency for all databases: 82.29
```
The _print_results_ have other options as well, please explore them.
If you are intrested in the performance statistics, you can either view the stats files or run a simple grep on the worker or SDS stats files:
```bash

e ## ToDo:VERAGE stats-sds*
stats-sds-psql1-vaelin-nkmld-cephnode-eastus21-5c7xj-fmdj2kswrt.log:AVERAGE EBS device nvme0n1 read/s: 2412.72, write/s: 6786.71, utilization%: 65.54
stats-sds-psql1-vaelin-nkmld-cephnode-eastus21-5c7xj-fmdj2kswrt.log:AVERAGE EBS device nvme1n1 read/s: 2398.97, write/s: 6549.72, utilization%: 59.72
stats-sds-psql1-vaelin-nkmld-cephnode-eastus21-5c7xj-fmdj2kswrt.log:AVERAGE RX: 27.89GB
stats-sds-psql1-vaelin-nkmld-cephnode-eastus21-5c7xj-fmdj2kswrt.log:AVERAGE TX: 16.61GB
stats-sds-psql1-vaelin-nkmld-cephnode-eastus21-5c7xj-fmdj2kswrt.log:AVERAGE CPU utilization: 32.87
stats-sds-psql1-vaelin-nkmld-cephnode-eastus21-5fv74-7fbqlz8f9b.log:AVERAGE EBS device nvme0n1 read/s: 2018.73, write/s: 7016.56, utilization%: 63.93
stats-sds-psql1-vaelin-nkmld-cephnode-eastus21-5fv74-7fbqlz8f9b.log:AVERAGE EBS device nvme1n1 read/s: 2231.00, write/s: 6310.89, utilization%: 63.61
stats-sds-psql1-vaelin-nkmld-cephnode-eastus21-5fv74-7fbqlz8f9b.log:AVERAGE RX: 27.67GB
stats-sds-psql1-vaelin-nkmld-cephnode-eastus21-5fv74-7fbqlz8f9b.log:AVERAGE TX: 21.90GB
stats-sds-psql1-vaelin-nkmld-cephnode-eastus21-5fv74-7fbqlz8f9b.log:AVERAGE CPU utilization: 31.28
stats-sds-psql1-vaelin-nkmld-cephnode-eastus21-8fwqv-gg89r8h5tm.log:AVERAGE EBS device nvme0n1 read/s: 2543.88, write/s: 6516.52, utilization%: 67.79
stats-sds-psql1-vaelin-nkmld-cephnode-eastus21-8fwqv-gg89r8h5tm.log:AVERAGE EBS device nvme1n1 read/s: 2132.71, write/s: 6859.18, utilization%: 67.01
stats-sds-psql1-vaelin-nkmld-cephnode-eastus21-8fwqv-gg89r8h5tm.log:AVERAGE RX: 27.83GB
stats-sds-psql1-vaelin-nkmld-cephnode-eastus21-8fwqv-gg89r8h5tm.log:AVERAGE TX: 20.01GB
stats-sds-psql1-vaelin-nkmld-cephnode-eastus21-8fwqv-gg89r8h5tm.log:AVERAGE CPU utilization: 26.62
```

### TODO (in no particular order of importance):
1. Adding PCP (Performance Co-Pilot) as a stats pods vs plain sysstat data. You will be able to choose between type of stats collection, but the PCP stats are going to be a lot more comprehensive and includes also graphs.
2. List of SDS devices per node (vs per cluster)
3. Adding MongoDB using YCSB
4. Adding redis using YCSB
5. Move to Python or at least have a version in python.
6. Maybe operatorize the scripts :)
7. Add the ability to use "-j run" without waiting for the jobs to finish and without collecting the logs.

\*(sherlock aschii image courtesy of hjm)
