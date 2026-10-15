#!/usr/bin/env bash
# Package NIKA into a fresh Incus VM for nika.snapshot benchmark runs and take its
# stateless snapshot `installed` (use `<vm>/installed` as nika.snapshot.base).
# Run on the host as a user in incus-admin, from the repository root.
# See docs/operations/vm-snapshots.md.
#
#   scripts/snapshot-base.sh <vm> [--cpus 16] [--memory 8GiB] [--prepare-all]
#
# --prepare-all runs `nika images prepare` in the guest (every benchmark image).
# Without it, the first lab deploy in each golden VM builds or pulls its images.
# Set NIKA_SNAPSHOT_TIMINGS=<file> to append the step timings as a JSON line.
set -euo pipefail

VM=${1:?usage: snapshot-base.sh <vm> [--cpus N] [--memory SIZE] [--prepare-all]}
shift
CPUS=16 MEMORY=8GiB PREPARE_ALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --cpus) CPUS=$2; shift 2 ;;
    --memory) MEMORY=$2; shift 2 ;;
    --prepare-all) PREPARE_ALL=1; shift ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
STATE=$(( ${MEMORY%GiB} + 2 ))GiB
OUT=${NIKA_SNAPSHOT_TIMINGS:-}
declare -A T
step() { T[$1]=$(date +%s.%N); }
g() { incus exec "$VM" -- "$@"; }
gu() { incus exec "$VM" -- su - ubuntu -c "cd ~/nika && $*"; }
wait_agent() { until incus exec "$VM" -- true 2>/dev/null; do sleep 1; done; }

step start
incus init images:ubuntu/24.04/cloud "$VM" --vm -c limits.cpu="$CPUS" -c limits.memory="$MEMORY" \
  -c migration.stateful=true -d root,size=80GiB -d root,size.state="$STATE" >/dev/null
incus start "$VM"
wait_agent
g cloud-init status --wait >/dev/null || true

step os_packages
g sh -c 'curl -fsSL https://get.docker.com | sh >/tmp/docker-install.log 2>&1 && usermod -aG docker ubuntu'
g sh -c 'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq socat linux-modules-extra-$(uname -r) >/dev/null'

step nika_install
bundle=$(mktemp --suffix=.bundle)
git bundle create "$bundle" HEAD >/dev/null 2>&1
incus file push "$bundle" "$VM/tmp/nika.bundle"
rm -f "$bundle"
g su - ubuntu -c 'git clone -q /tmp/nika.bundle ~/nika'
# Overlay the working tree so uncommitted changes match the host checkout.
git ls-files -co --exclude-standard -z | tar --null -cf - -T - |
  g su - ubuntu -c 'tar -xf - -C ~/nika'
gu "sg docker -c './scripts/install.sh --skip-images' >/tmp/install.log 2>&1"

step images
if [ "$PREPARE_ALL" = 1 ]; then
  gu "sg docker -c '.venv/bin/nika images prepare' >/tmp/images.log 2>&1"
  # A pull that fails quietly would make every golden build pull the image again.
  missing=$(gu "sg docker -c '.venv/bin/nika images list' | sed 's/@sha256:.*//' |
    while read -r img; do docker image inspect \"\$img\" >/dev/null 2>&1 || echo \"\$img\"; done")
  if [ -n "$missing" ]; then
    echo "error: images missing after nika images prepare (see /tmp/images.log in $VM):" >&2
    echo "$missing" >&2
    exit 1
  fi
fi

step guest_prep
# Freed pages read as zero, so they drop out of the RAM state file.
g sh -c 'echo "GRUB_CMDLINE_LINUX_DEFAULT=\"\$GRUB_CMDLINE_LINUX_DEFAULT init_on_free=1\"" > /etc/default/grub.d/99-nika-snapshot.cfg && update-grub >/dev/null 2>&1'
# Keep the restored clock on the snapshot timeline and drop idle services.
g systemctl mask chrony.service systemd-timesyncd.service cron.service networkd-dispatcher.service \
  packagekit.service polkit.service rsyslog.service unattended-upgrades.service getty@tty1.service \
  apt-daily.timer apt-daily-upgrade.timer dpkg-db-backup.timer e2scrub_all.timer fstrim.timer \
  logrotate.timer motd-news.timer ua-timer.timer systemd-tmpfiles-clean.timer >/dev/null 2>&1
gu "sed -i 's/gateway_port: *0/gateway_port: 8800/' config/nika.yaml"
incus file push - "$VM/etc/systemd/system/nika-remote.service" <<'EOF'
[Unit]
Description=NIKA remote daemon (loopback only; reached over vsock)
After=docker.service
Requires=docker.service
[Service]
User=ubuntu
Group=docker
WorkingDirectory=/home/ubuntu/nika
ExecStart=/home/ubuntu/nika/.venv/bin/nika remote serve --host 127.0.0.1 --port 8700
Restart=always
[Install]
WantedBy=multi-user.target
EOF
for port in 8700 8800; do
  incus file push - "$VM/etc/systemd/system/nika-vsock-$port.service" <<EOF
[Unit]
Description=Expose loopback port $port on vsock port $port
[Service]
ExecStart=/usr/bin/socat VSOCK-LISTEN:$port,fork,reuseaddr TCP:127.0.0.1:$port
Restart=always
[Install]
WantedBy=multi-user.target
EOF
done
# incus-agent re-announces itself only every 5 s; this makes incus exec usable sooner.
incus file push - "$VM/etc/systemd/system/nika-agent-ping.service" <<'EOF'
[Unit]
Description=Re-announce incus-agent status quickly after a stateful restore
After=incus-agent.service
[Service]
ExecStart=/bin/sh -c 'while :; do printf "STARTED\n" > /dev/virtio-ports/org.linuxcontainers.incus 2>/dev/null; sleep 0.2; done'
Restart=always
[Install]
WantedBy=multi-user.target
EOF
g systemctl daemon-reload
g systemctl enable nika-remote nika-vsock-8700 nika-vsock-8800 nika-agent-ping >/dev/null 2>&1
incus restart "$VM"
wait_agent
until g systemctl is-active -q nika-remote; do sleep 1; done

step snapshot
# A stateless snapshot of a running VM only sees what the guest has written back.
g sync
incus snapshot create "$VM" installed
step end

python3 - "$OUT" "$VM" "$CPUS" "$MEMORY" "$PREPARE_ALL" \
  "${T[start]}" "${T[os_packages]}" "${T[nika_install]}" "${T[images]}" \
  "${T[guest_prep]}" "${T[snapshot]}" "${T[end]}" <<'EOF'
import json, sys, time
out, vm, cpus, memory, prepare_all, *ts = sys.argv[1:]
ts = [float(t) for t in ts]
names = ["vm_boot", "os_packages", "nika_install", "images", "guest_prep", "snapshot"]
row = {"kind": "packaging", "vm": vm, "cpus": cpus, "memory": memory,
       "prepare_all": prepare_all == "1",
       **{f"{n}_sec": round(b - a, 1) for n, a, b in zip(names, ts, ts[1:])},
       "total_sec": round(ts[-1] - ts[0], 1), "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
if out:
    with open(out, "a") as fh:
        fh.write(json.dumps(row) + "\n")
print(f"{vm}/installed ready in {row['total_sec']:.0f}s: "
      + ", ".join(f"{n} {row[n + '_sec']:.0f}s" for n in names))
EOF
