# sherlock

Like the great detective, the idea behind the set of scripts in this repo is to investigate performance of Software Define Storage (SDS) using generic database workloads.

The scripts are meant to be used with OpenShift/Kubernetes.

The script are using containers that are design specifically to run database benchmarks and collect statistics from the cluster nodes.

The idea behind this project is to help people not only run the tests, but also make it easy to create the environment (the databases) to run the tests, because of that, the project is not asking for any kind of stateful application (like a DB) to store the data, but instead is using wherever you are running the scripts from as where the tests data will be stored.

The KISS mentality was the reason for bash. it just exists pretty much on any OS these days.

## Workloads status

sysbench: works on MySQL and PostgreSQL.

pgbench: works on PostgreSQL (TCP-B mostly writes by default)

YCSB: works on MongoDB

hammerdb: works on SQL Server (In Development)

## Important points:
1. obviously you need the kubectl or oc clients locally to run this
2. when you are actually running a workload (like in the "run" part of sysbench or pgbench, or when using fio), the script will use the kubectl "wait" function to monitor when all jobs are done and retrieve all logs. even if you loose connecetivty or get logged out from the OCP/k8s cluster, the jobs data still reside as long as the jobs are not deleted, so you can retreive the data later on. Just make sure to remember the RUN_NAME of your run and use the "get_run_name_logs" script.
3. if you are using the stats option, you will need to add hostnetwork and privileged SCC to whatever user and namespace running the pods:
oc adm policy add-scc-to-user hostnetwork -npostgresql -z default
oc adm policy add-scc-to-user privileged -npostgresql -z default
