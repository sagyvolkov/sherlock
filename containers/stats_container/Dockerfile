FROM registry.fedoraproject.org/fedora-minimal:35
RUN microdnf install -y sysstat procps bc net-tools iproute vi && microdnf clean all
RUN mkdir /stats
WORKDIR /stats
ADD ./scripts ./
CMD tail -f /dev/null
