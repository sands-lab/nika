#!/bin/bash
# One role of the DHCP clock-step emulation; run.sh starts it in its netns.
# Every dhcpd/dhclient runs under libfaketime reading the shared offset file
# /ft/offset (re-read on every call, monotonic clock left alone), so writing
# "+6d" there steps their wall clock at once, like `date --set` in the guest.
# Usage: role.sh server|client <name> [peer-name]
set -u
role=$1 name=$2 peer=${3:-}
STEP_AT=75      # seconds after server start: control window on the unstepped clock
STEP=+6d        # age of lab image b7cdaab3d6f9 at the time of the check
END_AT=330
IF=eth1
LIB=/usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1
# For short-lived `date` calls: the shared offset file.
FT=(env LD_PRELOAD=$LIB FAKETIME_TIMESTAMP_FILE=/ft/offset FAKETIME_NO_CACHE=1 FAKETIME_DONT_FAKE_MONOTONIC=1)
up() { cut -d' ' -f1 /proc/uptime; }
# For dhcpd/dhclient: the file offset did not reach a running dhclient (run 1),
# so each daemon gets +6d after a per-process delay that lands on the shared
# step instant /ft/step_at (host uptime seconds).
daemon_ft() {
  local wait=$(awk -v s="$(cat /ft/step_at)" -v n="$(up)" 'BEGIN{printf "%d", s-n+0.5}')
  echo env LD_PRELOAD=$LIB FAKETIME=$STEP FAKETIME_START_AFTER_SECONDS=$wait FAKETIME_DONT_FAKE_MONOTONIC=1
}
stamp() { while IFS= read -r l; do echo "mono=$(up) $l"; done; }
since() { awk -v a="$(up)" -v b="$1" -v s="$2" 'BEGIN{exit !(a-b < s)}'; }

if [ "$role" = server ]; then
  # campus_lan's dhcpd.conf (lab_dhcp.py), one subnet, no relay.
  cat > /etc/dhcp/dhcpd.conf <<EOF
default-lease-time 30;
max-lease-time 60;
authoritative;
subnet 10.250.77.0 netmask 255.255.255.0 {
    range 10.250.77.10 10.250.77.100;
    option routers 10.250.77.2;
}
EOF
  : > /var/lib/dhcp/dhcpd.leases
  awk -v n="$(up)" 'BEGIN{printf "%.2f\n", n+'$STEP_AT'}' > /ft/step_at
  $(daemon_ft) dhcpd -d -4 -cf /etc/dhcp/dhcpd.conf -lf /var/lib/dhcp/dhcpd.leases \
    -pf /run/dhcpd.pid $IF 2>&1 | stamp > /ft/dhcpd.log &
  t0=$(up)
  echo "mono=$t0 server start" >> /ft/timeline.log
  touch /ft/ready
  while since "$t0" $STEP_AT; do sleep 0.5; done
  cp /var/lib/dhcp/dhcpd.leases /ft/dhcpd.leases.prestep
  echo "$STEP" > /ft/offset
  echo "mono=$(up) STEP offset=$STEP fake_wall=$("${FT[@]}" date -u +%FT%TZ) real_wall=$(date -u +%FT%TZ)" >> /ft/timeline.log
  while since "$t0" $END_AT; do sleep 0.5; done
  cp /var/lib/dhcp/dhcpd.leases /ft/dhcpd.leases.end
  echo "mono=$(up) end" >> /ft/timeline.log
  touch /ft/done
else
  while [ ! -f /ft/ready ]; do sleep 0.5; done
  $(daemon_ft) dhclient -d -v -cf /etc/dhcp/dhclient.conf -lf /var/lib/dhcp/dhclient-$name.leases \
    -pf /run/dhclient-$name.pid $IF 2>&1 | stamp > /ft/dhclient-$name.log &
  last=""
  while [ ! -f /ft/done ]; do
    addr=$(ip -4 -o addr show $IF | awk '{print $4}' | tr '\n' ' ')
    gw=$(ip -4 route show default dev $IF | awk '{print $3}')
    echo "$addr" | awk -F/ '{print $1}' > /ft/.addr-$name && mv /ft/.addr-$name /ft/addr-$name
    line="addr=[${addr% }] gw=${gw:-none}"
    if [ -n "$peer" ]; then
      pip=$(cat /ft/addr-$peer 2>/dev/null)
      if [ -n "$pip" ] && ping -c1 -W1 -q "$pip" >/dev/null 2>&1; then
        line="$line ping($peer $pip)=ok"
      else
        line="$line ping($peer ${pip:-noaddr})=FAIL"
      fi
    fi
    [ "$line" != "$last" ] && echo "mono=$(up) fake_wall=$("${FT[@]}" date -u +%FT%TZ) $line" >> /ft/state-$name.log
    last=$line
    sleep 0.5
  done
  cp /var/lib/dhcp/dhclient-$name.leases /ft/ 2>/dev/null
fi
