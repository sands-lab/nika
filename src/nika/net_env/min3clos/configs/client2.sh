#!/bin/bash

cat > /etc/network/interfaces << EOF
auto eth1

iface eth1 inet static
  address 10.0.0.27
  netmask 255.255.255.254

iface eth1 inet6 static
  address 1000:10:0:0::27
  netmask 127
  pre-up echo 0 > /proc/sys/net/ipv6/conf/eth1/accept_ra
EOF

ifup eth1
# Containerlab veths default to MTU 9500, above the SR Linux port MTU (9232),
# which drops oversized frames without ICMP and black-holes full-size TCP.
ip link set dev eth1 mtu 1500
ip route replace default via 10.0.0.26 dev eth1
