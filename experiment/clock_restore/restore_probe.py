"""Restore a copy of a campus_lan lab image and watch DHCP across the clock step.

Mirrors TrialLab._restore on origin/dev (clone disk, start Firecracker, page
server when huge pages are on, load the snapshot, wait for the daemon), but
probes the guest before and after the `date --set=@<host time>` step that
origin/dev runs right after the daemon answers. Never touches the image; the
copy lives in its own trial directory and is released at the end.

Usage: uv run python restore_probe.py <image_key> <log_path>
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from nika.firecracker.config import firecracker_settings
from nika.firecracker.image import DAEMON_PORT, MEMORY, STATE, _clone, root
from nika.firecracker.trial import _start_page_server, release_trial, trial_dir
from nika.firecracker.vmm import (
    DISK,
    VSOCK,
    Firecracker,
    firecracker_binary,
    guest_exec,
    pid_alive,
    wait_http_ok,
)

TRIAL_KEY = "dhcp-clock-check-20261110"

PROBE = r"""
echo "### guest wall=$(date -u +%FT%T.%3NZ) epoch=$(date +%s) uptime=$(cut -d' ' -f1 /proc/uptime)"
for c in $(docker ps --format '{{.Names}}' | sort); do
  short=$(echo "$c" | sed -E 's/^kathara_[^_]+_//; s/_[^_]+$//')
  if docker exec "$c" pgrep -x dhcpd >/dev/null 2>&1; then
    echo "## SERVER $short"
    docker exec "$c" sh -c 'pgrep -a dhcpd; awk "/^lease/{ip=\$2} /starts|ends|binding state|hardware ethernet/{printf \"%s %s\\n\", ip, \$0}" /var/lib/dhcp/dhcpd.leases | tail -n 60'
  fi
  if docker exec "$c" pgrep -x dhclient >/dev/null 2>&1; then
    echo "## HOST $short $(docker exec "$c" ip -4 -o addr show eth0 | awk '{print $4}' | tr '\n' ' ')| dflt=$(docker exec "$c" ip -4 route show default | awk '{print $3}')"
    docker exec "$c" sh -c 'tail -n 14 /var/lib/dhcp/dhclient.leases 2>/dev/null | grep -E "fixed-address|dhcp-lease-time|renew|rebind|expire"' | tr '\n' ';' ; echo
  elif docker exec "$c" sh -c 'grep -q "dhclient" /etc/*.startup 2>/dev/null || test -f /var/lib/dhcp/dhclient.leases' 2>/dev/null; then
    echo "## HOST-NO-DHCLIENT $short $(docker exec "$c" ip -4 -o addr show eth0 | awk '{print $4}' | tr '\n' ' ')"
  fi
done
"""

PING = r"""
hosts=$(for c in $(docker ps --format '{{.Names}}' | sort); do
  docker exec "$c" test -f /var/lib/dhcp/dhclient.leases 2>/dev/null && echo "$c"; done)
src=$(echo "$hosts" | head -1)
echo "### ping from $(echo $src | sed -E 's/^kathara_[^_]+_//; s/_[^_]+$//') at $(date -u +%T)"
for c in $hosts; do
  ip=$(docker exec "$c" ip -4 -o addr show eth0 | awk '{split($4,a,"/"); print a[1]}' | head -1)
  [ -z "$ip" ] && { echo "$c NOADDR"; continue; }
  r=$(docker exec "$src" ping -c 2 -W 1 -q "$ip" | grep -o '[0-9]* received')
  echo "$(echo $c | sed -E 's/^kathara_[^_]+_//; s/_[^_]+$//') $ip $r"
done
"""

START_CAPTURE = r"""
for c in $(docker ps --format '{{.Names}}'); do
  if docker exec "$c" pgrep -x dhcpd >/dev/null 2>&1; then
    docker exec -d "$c" sh -c 'timeout 600 tcpdump -n -tttt -l -v -i eth0 udp port 67 or udp port 68 > /tmp/dhcp-capture.txt 2>&1'
    echo "capture on $c"
  fi
done
"""

COLLECT = r"""
for c in $(docker ps --format '{{.Names}}'); do
  if docker exec "$c" pgrep -x dhcpd >/dev/null 2>&1; then
    echo "## capture (server $c)"
    docker exec "$c" sh -c 'pkill tcpdump; sleep 1; grep -E "^[0-9]{4}-|DHCP-Message|Requested-IP|Client-IP|Your-IP|Client-Ethernet|Lease-Time" /tmp/dhcp-capture.txt'
    echo "## dhcpd logs"
    docker exec "$c" sh -c 'ls /var/log; grep -h dhcpd /var/log/syslog /var/log/messages /var/log/daemon.log 2>/dev/null | tail -n 200; journalctl -u isc-dhcp-server --no-pager 2>/dev/null | tail -n 200'
  fi
done
"""


def main() -> None:
    image_key, log_path = sys.argv[1], Path(sys.argv[2])
    log = open(log_path, "a")

    def out(title: str, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        log.write(f"\n===== [{stamp} host] {title}\n{text}\n")
        log.flush()
        print(f"[{stamp}] {title}", flush=True)

    image = root() / "images" / image_key
    run = trial_dir(TRIAL_KEY)
    assert not run.exists(), run
    settings = firecracker_settings()
    run.mkdir(parents=True)
    try:
        _clone(image / DISK, run / DISK)
        vm = Firecracker(run, firecracker_binary())
        vm.start()
        uffd = server = None
        if settings.huge_pages:
            uffd = run / "uffd.sock"
            server = _start_page_server(run, uffd, image / MEMORY)
        vm.load(state=image / STATE, memory=image / MEMORY, uffd_socket=uffd)
        uds = run / VSOCK
        wait_http_ok(
            uds,
            DAEMON_PORT,
            alive=lambda: (
                (server is None or server.poll() is None)
                and pid_alive(int(vm.pid_file.read_text()))
            ),
        )
        out(f"restored {image_key}; huge_pages={settings.huge_pages}", "")
        out("start capture", guest_exec(uds, START_CAPTURE, timeout=120))
        out("PRE-STEP probe (t=0)", guest_exec(uds, PROBE, timeout=300))
        out("PRE-STEP ping", guest_exec(uds, PING, timeout=300))
        time.sleep(40)  # control: one renewal interval on the snapshot clock
        out("PRE-STEP probe (t=+40s, no step yet)", guest_exec(uds, PROBE, timeout=300))
        guest_exec(uds, f"date --set=@{time.time():.6f} >/dev/null", timeout=30)
        out("STEP done (date --set=@host time, as origin/dev trial.py:167)", "")
        step = time.monotonic()
        for at in (0, 5, 20, 45, 90, 180):
            time.sleep(max(0.0, step + at - time.monotonic()))
            out(f"POST-STEP probe (+{at}s)", guest_exec(uds, PROBE, timeout=300))
            if at in (0, 20, 90, 180):
                out(f"POST-STEP ping (+{at}s)", guest_exec(uds, PING, timeout=300))
        out("collect", guest_exec(uds, COLLECT, timeout=300))
    finally:
        release_trial(TRIAL_KEY)
        out("released trial copy", str(run))


if __name__ == "__main__":
    main()
