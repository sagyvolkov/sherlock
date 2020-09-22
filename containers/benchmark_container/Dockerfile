FROM registry.access.redhat.com/ubi8:latest

ADD ./mongo-yum /etc/yum.repos.d/
ADD ./ycsb /tmp/
RUN rpm -Uvh https://yum.postgresql.org/11/redhat/rhel-7-x86_64/pgdg-redhat-repo-latest.noarch.rpm
RUN dnf -y install postgresql11-contrib --disablerepo rhel-8-for-x86_64-appstream-rpms; dnf clean all
RUN dnf -y install mysql net-tools mysql-libs mysql-devel libtool make which git bind-utils; dnf clean all
RUN dnf -y install java-1.8.0-openjdk mongodb-org-shell; dnf clean all
RUN curl -s https://packagecloud.io/install/repositories/akopytov/sysbench/script.rpm.sh | bash
RUN dnf -y install sysbench; dnf clean all
RUN tar zxvf /tmp/mongo-only-ycsb-0.15.0.gz
RUN mv ycsb-0.15.0 ycsb
RUN rm /tmp/mongo-only-ycsb-0.15.0.gz

RUN dnf install -y --nodocs tcl unixODBC
RUN curl https://packages.microsoft.com/config/rhel/8/prod.repo -o /etc/yum.repos.d/mssql-release.repo
ADD ./centos8.repo /etc/yum.repos.d/
RUN ACCEPT_EULA=Y dnf -y install --nodocs mssql-tools msodbcsql17 --enablerepo=centos8
RUN dnf clean all

COPY HammerDB-3.3-Linux-x86-64-Install HammerDB-3.3-Linux-x86-64-Install
RUN mkdir /hammerdb
RUN chmod 755 HammerDB-3.3-Linux-x86-64-Install
RUN ./HammerDB-3.3-Linux-x86-64-Install --prefix /hammerdb --mode silent

RUN mkdir scripts
WORKDIR scripts
ADD ./runs ./

CMD tail -f /dev/null
