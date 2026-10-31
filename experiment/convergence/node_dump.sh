# Dump one lab node's forwarding state as tagged sections (@@@ name).
# Runs inside the node with /bin/sh; probes that the node lacks are skipped.
sec() { name=$1; shift; echo "@@@ $name"; "$@" 2>/dev/null; echo; }
have() { command -v "$1" >/dev/null 2>&1; }

if have ip; then
  sec addr ip -j addr show
  sec route4 ip -j -4 route show table all
fi
if have sr_cli; then
  # SR Linux forwards in its own datapath; its route table is the FIB.
  sec srlfib sr_cli "info from state network-instance default route-table | as json"
fi
if have sr_cli || { have vtysh && vtysh -c 'show version' >/dev/null 2>&1; }; then
  echo "@@@ router"
fi
