#!/bin/bash
# Check whether libfaketime reaches dhclient: run one dhclient with a fixed
# FAKETIME offset (no file), and one with the shared-file mode used by role.sh,
# and print the renew/expire dates each one writes.
set -u
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null && apt-get install -y -qq faketime binutils >/dev/null
LIB=/usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1
echo "real date: $(date -u)"
echo "faked date (env):  $(LD_PRELOAD=$LIB FAKETIME=+6d date -u)"
echo "+6d" > /tmp/off
echo "faked date (file): $(LD_PRELOAD=$LIB FAKETIME_TIMESTAMP_FILE=/tmp/off FAKETIME_NO_CACHE=1 date -u)"
nm -D /sbin/dhclient | grep -E " (gettimeofday|clock_gettime|time|__clock_gettime)" ; echo "dhcpd:"; nm -D /usr/sbin/dhcpd | grep -E " (gettimeofday|clock_gettime|time)$"
ip link add br-t type bridge && ip link set br-t up
for ns in srv c1 c2; do
  ip netns add $ns; ip link add v-$ns type veth peer name eth1 netns $ns
  ip link set v-$ns master br-t up; ip netns exec $ns ip link set lo up; ip netns exec $ns ip link set eth1 up
done
ip netns exec srv ip addr add 10.250.77.2/24 dev eth1
printf 'default-lease-time 30;\nmax-lease-time 60;\nauthoritative;\nsubnet 10.250.77.0 netmask 255.255.255.0 { range 10.250.77.10 10.250.77.100; }\n' > /etc/dhcp/dhcpd.conf
: > /var/lib/dhcp/dhcpd.leases
ip netns exec srv dhcpd -4 -q -cf /etc/dhcp/dhcpd.conf -pf /run/dhcpd.pid eth1
ip netns exec c1 env LD_PRELOAD=$LIB FAKETIME=+6d dhclient -d -v -lf /tmp/c1.leases -pf /run/c1.pid eth1 > /tmp/c1.out 2>&1 &
ip netns exec c2 env LD_PRELOAD=$LIB FAKETIME_TIMESTAMP_FILE=/tmp/off FAKETIME_NO_CACHE=1 dhclient -d -v -lf /tmp/c2.leases -pf /run/c2.pid eth1 > /tmp/c2.out 2>&1 &
sleep 6; grep -h bound /tmp/c1.out /tmp/c2.out; for p in $(pgrep dhclient); do echo "pid $p LD_PRELOAD: $(tr '\0' '\n' < /proc/$p/environ | grep -c LD_PRELOAD) maps: $(grep -c faketime /proc/$p/maps)"; done
echo "dhcpd server leases:"; grep -E "^lease|starts" /var/lib/dhcp/dhcpd.leases
echo "c1 (env):";  grep -E "renew|expire" /tmp/c1.leases
echo "c2 (file):"; grep -E "renew|expire" /tmp/c2.leases
