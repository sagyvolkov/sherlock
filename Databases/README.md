## Kubernetes infrastructure and databases

So let's talk about infrastructure resources and databases.
The idea behind this tool is to provide an easy way to deploy databases in OCP/k8s environment and then stress test the Software Define Storage (SDS) that these DBs are using.

Why do resources matter? when Sherlock is creating database deployments the database pods always have resources.requests = resources.limits, this way, each database have a granted infrastructure resources from the node it resides on and via cgroups making sure each database on each node have its own litte "domain" of resources.

However, this also means that you need to do some basic calculations and make sure that these resources are actually available.
The best way to look at it is as if the databases are the only application running on the cluster (beside of course the Kubernetes resources and the SDS resources) - the idea of the tool is to stress test SDSs so we want either as many databases that we can "squeeze" from each node or very few very large databases.
Remember that the tool does not count on the k8s scheduler to "spread" equally the databases in the cluster (experience and experiments showed that when dealing with many databases per node, they are not evenly "spread" across all nodes), hence specifying NUMBER_OF_WORKERS is important (and why it is separated from the WORKERS_LIST_FILE) so the tool will setup the same number of databases per worker node.

## Some practical examples:
lets say each node in the cluster that will be running the databases have 20 cores and 128GB of RAM. if DB_POD_MEM=16Gi, DB_POD_CPU=2, we can probably easily fit 6 databases and go even to 8 databases (Per Openshift documentation you should leave ~10% of your resources per node for OCP/k8s resources).
if you will, try to deploy 10 databases per node on the same given nodes from above, Sherlock will probably fail to create the last 2-4 databases deployments per nodes (whatever was created successfully can still be used and you can just adjust these variables to how DBs you were able to "squeeze" in each node).

Also, make sure you have enough storage to provide PVC_SIZE * DB_PER_WORKER * NUMBER_OF_WORKERS to the databases (for details on these variables look at the sherlock.config file).

## Storage vs Memory
Since Sherlock was created to measure SDS performance, you always need to make sure large portion of your database actually reside "on-disk"/on-the-storage, if you don't you are not really stressing the storage. For example, if I have create 4 MySQL databases on each node, each pod with memory resource of 4Gi and the database size is only 6GB, there is a good chance that a lot of the IOs will "look" to be very fast since they are mostly done in RAM.
as a good rule to stress storage, make sure that only 10%-20% of your database size can fit the pod memory resources (and of course, also create enough databases per node or large enough database per node that the database won't "sit" entirely in the Linux buffer cache)

## Benchmarks
In the storage world, to show storage performance capabilities, you want to aim to show a 70% read 30% writes ratio (or some variation like 75r/25w or 80r/20w), but not all benchmark are created equally, for example, hammerdb is based on TPC-C and it does mostly writes (you can play with the number of warehouses and see the impact on the devices used for by the SDS). The same goes for PGBENCH (based on TPC-B). Sysbench is more easily "manipulated" to get to 70r/30w ratio.
