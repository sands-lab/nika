FROM ubuntu:22.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    git mininet openvswitch-switch openvswitch-testcontroller iproute2 iputils-ping net-tools python3 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/ovs-testcontroller /usr/bin/ovs-controller
WORKDIR /work
