FROM centos:centos7
ADD ./mongo-yum /etc/yum.repos.d/
ADD ./ycsb /tmp/
RUN yum -y update; yum clean all
RUN rpm -Uvh https://yum.postgresql.org/11/redhat/rhel-7-x86_64/pgdg-redhat-repo-latest.noarch.rpm
RUN yum -y install postgresql11-contrib curl; yum clean all
RUN yum -y install mysql net-tools mysql-libs mysql-devel libtool make which git bind-utils; yum clean all
RUN yum -y install java-1.8.0-openjdk mongodb-org-shell; yum clean all
RUN curl -s https://packagecloud.io/install/repositories/akopytov/sysbench/script.rpm.sh | bash
RUN yum -y install sysbench; yum clean all
RUN tar zxvf /tmp/mongo-only-ycsb-0.15.0.gz
RUN mv ycsb-0.15.0 ycsb
RUN rm /tmp/mongo-only-ycsb-0.15.0.gz
RUN mkdir scripts
WORKDIR scripts
ADD ./runs ./

CMD tail -f /dev/null
