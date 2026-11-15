#!/bin/bash
# Container entry point: install libfaketime, build srv/c1/c2 network
# namespaces on an in-container bridge, run the roles, wait, and leave the
# logs in /ft (bind-mounted to ~/dhcp-clock-check/emu-run).
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null && apt-get install -y -qq faketime >/dev/null
faketime --version 2>&1 | head -1 > /ft/versions.txt || true
{ dhcpd --version; dhclient --version; } >> /ft/versions.txt 2>&1
echo "+0" > /ft/offset
printf 'timeout 1;\nretry 1;\n' >> /etc/dhcp/dhclient.conf   # as campus_lan hosts
ip link add br-t type bridge && ip link set br-t up
for ns in srv c1 c2; do
  ip netns add $ns
  ip link add v-$ns type veth peer name eth1 netns $ns
  ip link set v-$ns master br-t up
  ip netns exec $ns ip link set lo up
  ip netns exec $ns ip link set eth1 up
done
ip netns exec srv ip addr add 10.250.77.2/24 dev eth1
ip netns exec srv bash /emu/role.sh server srv &
ip netns exec c1 bash /emu/role.sh client c1 c2 &
ip netns exec c2 bash /emu/role.sh client c2 c1 &
wait
chmod -R a+rwX /ft
