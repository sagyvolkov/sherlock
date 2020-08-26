FROM registry.fedoraproject.org/fedora-minimal:31
RUN microdnf install sysstat procps bc net-tools vi && microdnf clean all
RUN mkdir /stats
WORKDIR /stats
ADD ./scripts ./
CMD tail -f /dev/null
