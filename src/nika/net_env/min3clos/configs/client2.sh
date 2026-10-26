#!/bin/bash
set -e

echo 0 > /proc/sys/net/ipv6/conf/eth1/accept_ra
ip addr replace 10.0.0.27/31 dev eth1
ip -6 addr replace 1000:10:0:0::27/127 dev eth1
# Containerlab veths default to MTU 9500, above the SR Linux port MTU (9232),
# which drops oversized frames without ICMP and black-holes full-size TCP.
ip link set dev eth1 mtu 1500 up
ip route replace default via 10.0.0.26 dev eth1
ip -6 route replace default via 1000:10::26 dev eth1
# Data set the other client syncs.
mkdir -p /var/www && truncate -s 8M /var/www/large.bin
