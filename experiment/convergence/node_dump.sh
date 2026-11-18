# Dump one lab node's forwarding state as tagged sections (@@@ name).
# Runs inside the node with /bin/sh; probes that the node lacks are skipped.
sec() { name=$1; shift; echo "@@@ $name"; "$@" 2>/dev/null; echo; }
have() { command -v "$1" >/dev/null 2>&1; }

if have ip; then
  sec addr ip -j addr show
  sec route4 ip -j -4 route show table all
  sec neighbor ip -j neighbor show
  sec nexthop ip -j nexthop show
  sec links ip -j -s link show
fi
sec clock sh -c 'date -u +%FT%T.%NZ; cat /proc/uptime'
if have conntrack; then sec conntrack conntrack -L; fi
if have tc; then sec qdisc tc -s qdisc show; fi
if have wg; then sec wireguard wg show; fi
if have vtysh; then
  sec bgp vtysh -c 'show bgp summary json'
  sec ospf vtysh -c 'show ip ospf neighbor json'
  sec isis vtysh -c 'show isis neighbor json'
fi
for lease in /var/lib/dhcp/dhcpd.leases /var/lib/dhcp/dhclient.leases; do
  if [ -r "$lease" ]; then sec "$lease" cat "$lease"; fi
done
if have kubectl; then
  sec pods kubectl get pods -A -o wide --request-timeout=5s
  sec leases kubectl get leases -A -o json --request-timeout=5s
fi
if have sr_cli; then
  # SR Linux forwards in its own datapath; its route table is the FIB.
  sec srlfib sr_cli "info from state network-instance default route-table | as json"
fi
if have sr_cli || { have vtysh && vtysh -c 'show version' >/dev/null 2>&1; }; then
  echo "@@@ router"
fi
