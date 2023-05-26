# RHEL8 based image
FROM registry.access.redhat.com/ubi8:latest

# Adding all the repos and files needed
ADD ./mongo-org-6.0.repo /etc/yum.repos.d/
ADD ./ycsb /tmp/

# Installing MongoDB shell and Java
RUN dnf -y install java-1.8.0-openjdk mongodb-mongosh

# Installing YCSB
RUN tar zxvf /tmp/ycsb-0.17.0_mongo_redis.tgz
RUN mv ycsb-0.17.0_mongo_redis ycsb
RUN rm -f /tmp/ycsb-0.17.0_mongo_redis.tgz

RUN dnf clean all -y

RUN mkdir scripts
WORKDIR scripts
ADD ./workload ./


CMD tail -f /dev/null
