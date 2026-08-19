#!/bin/bash
# Keep PID 1 alive after ONOS exits so crash / southbound faults stay injectable.
set -u
if [[ -x /root/onos/bin/onos-service ]]; then
  /root/onos/bin/onos-service server &
  onos_pid=$!
  wait "${onos_pid}" || true
elif [[ -x /root/onos/apache-karaf-*/bin/karaf ]]; then
  # Fallback for alternate ONOS layouts.
  /root/onos/apache-karaf-*/bin/karaf server &
  onos_pid=$!
  wait "${onos_pid}" || true
fi
exec sleep infinity
