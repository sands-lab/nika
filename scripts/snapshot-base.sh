#!/usr/bin/env bash
# Build the Firecracker base image for nika.snapshot benchmark runs.
# Run from the repository root as a user with sudo, Docker and /dev/kvm access,
# after `nika images prepare` on this host. See docs/operations/vm-snapshots.md.
#
#   scripts/snapshot-base.sh [--root /var/lib/nika-snapshots] [--size 80G]
#
# 1. As root: unpack the Ubuntu 24.04 cloud root filesystem into an ext4 image
#    and install the kernel, Docker, socat and the guest services in a chroot.
# 2. As you: boot it in Firecracker, run scripts/install.sh --skip-images in the
#    guest (network through a host proxy over vsock), and load this host's
#    runtime images into the guest's Docker.
# Writes <root>/base/{rootfs.img,vmlinux,initrd.img,base.json}; golden VMs built
# from an older base are rebuilt on the next run.
set -euo pipefail

ROOT=/var/lib/nika-snapshots SIZE=80G
while [ $# -gt 0 ]; do
  case "$1" in
    --root) ROOT=$2; shift 2 ;;
    --size) SIZE=$2; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
REPO=$(pwd)
BASE=$ROOT/base
WORK=$ROOT/base.build
URL=https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-amd64-root.tar.xz
CACHE=$REPO/.nika_cache/snapshot
log() { printf '+ %s\n' "$*"; }

mkdir -p "$CACHE" "$WORK"
TARBALL=$CACHE/$(basename "$URL")
[ -f "$TARBALL" ] || { log "downloading $URL"; curl -fL --retry 3 -o "$TARBALL.part" "$URL"; mv "$TARBALL.part" "$TARBALL"; }

log "creating $SIZE ext4 root filesystem"
rm -f "$WORK/rootfs.img"
truncate -s "$SIZE" "$WORK/rootfs.img"
mkfs.ext4 -q -L nika-root "$WORK/rootfs.img"
MNT=$(mktemp -d)
cleanup() {
  for m in dev/pts dev proc sys run; do sudo umount -l "$MNT/$m" 2>/dev/null || true; done
  sudo umount -l "$MNT" 2>/dev/null || true
  rmdir "$MNT" 2>/dev/null || true
}
trap cleanup EXIT
sudo mount -o loop "$WORK/rootfs.img" "$MNT"
sudo tar -xJf "$TARBALL" -C "$MNT" --numeric-owner
for m in dev dev/pts proc sys run; do sudo mount --bind "/$m" "$MNT/$m"; done
sudo cp -L /etc/resolv.conf "$MNT/etc/resolv.conf.host"

log "installing kernel, Docker and guest services (chroot)"
sudo chroot "$MNT" /bin/bash -euo pipefail <<'CHROOT'
mount --bind /etc/resolv.conf.host /etc/resolv.conf 2>/dev/null || cp /etc/resolv.conf.host /etc/resolv.conf
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends linux-image-generic initramfs-tools \
  ca-certificates curl git socat sudo kmod iproute2 iputils-ping python3 >/dev/null
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin >/dev/null
apt-get clean
useradd -m -s /bin/bash -G docker,sudo ubuntu
echo 'ubuntu ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/90-ubuntu
echo nika-lab > /etc/hostname
echo 'LABEL=nika-root / ext4 defaults,noatime 0 1' > /etc/fstab
touch /etc/cloud/cloud-init.disabled
umount /etc/resolv.conf 2>/dev/null || true
rm -f /etc/resolv.conf.host

cat > /usr/local/bin/nika-guest-exec <<'EOF'
#!/bin/bash
# One command per vsock connection: a base64 script line, a byte-count line,
# then that many bytes of stdin for the script. Output ends with the exit status.
read -r script
read -r size
head -c "$size" | bash -c "$(printf '%s' "$script" | base64 -d)" 2>&1
printf '\n__nika_exit=%d\n' "${PIPESTATUS[1]}"
EOF
chmod 755 /usr/local/bin/nika-guest-exec
unit() { cat > "/etc/systemd/system/$1.service"; systemctl enable "$1" >/dev/null 2>&1; }
unit nika-guest-exec <<'EOF'
[Unit]
Description=NIKA guest exec on vsock 9000 (reached only through the VM's host-side socket)
[Service]
ExecStart=/usr/bin/socat VSOCK-LISTEN:9000,fork,reuseaddr EXEC:/usr/local/bin/nika-guest-exec
Restart=always
[Install]
WantedBy=multi-user.target
EOF
unit nika-remote <<'EOF'
[Unit]
Description=NIKA remote daemon (loopback only; reached over vsock)
After=docker.service
Requires=docker.service
ConditionPathExists=/home/ubuntu/nika/.venv/bin/nika
[Service]
User=ubuntu
Group=docker
WorkingDirectory=/home/ubuntu/nika
ExecStart=/home/ubuntu/nika/.venv/bin/nika remote serve --host 127.0.0.1 --port 8700
Restart=always
RestartSec=2
[Install]
WantedBy=multi-user.target
EOF
for port in 8700 8800; do
  unit nika-vsock-$port <<EOF
[Unit]
Description=Expose loopback port $port on vsock port $port
[Service]
ExecStart=/usr/bin/socat VSOCK-LISTEN:$port,fork,reuseaddr TCP:127.0.0.1:$port
Restart=always
[Install]
WantedBy=multi-user.target
EOF
done
# Keep the restored clock on the snapshot timeline and drop idle services.
systemctl mask systemd-timesyncd.service cron.service apt-daily.timer apt-daily-upgrade.timer \
  dpkg-db-backup.timer e2scrub_all.timer fstrim.timer logrotate.timer motd-news.timer \
  systemd-networkd-wait-online.service unattended-upgrades.service >/dev/null 2>&1 || true
CHROOT

log "copying the NIKA checkout"
sudo mkdir -p "$MNT/home/ubuntu/nika"
git -C "$REPO" ls-files -co --exclude-standard -z | tar --null -cf - -T - | sudo tar -xf - -C "$MNT/home/ubuntu/nika"
# Golden builds sync code against this list (the guest has no git checkout).
git -C "$REPO" ls-files -co --exclude-standard -z | sudo tee "$MNT/home/ubuntu/nika/.nika-files" >/dev/null
sudo chroot "$MNT" chown -R ubuntu:ubuntu /home/ubuntu/nika

log "extracting the kernel"
KVER=$(ls "$MNT/lib/modules" | sort -V | tail -1)
EXTRACT=/usr/src/linux-headers-$(uname -r)/scripts/extract-vmlinux
[ -x "$EXTRACT" ] || { EXTRACT=$CACHE/extract-vmlinux; curl -fsSL -o "$EXTRACT" https://raw.githubusercontent.com/torvalds/linux/master/scripts/extract-vmlinux; chmod +x "$EXTRACT"; }
sudo "$EXTRACT" "$MNT/boot/vmlinuz-$KVER" > "$WORK/vmlinux"
sudo cp "$MNT/boot/initrd.img-$KVER" "$WORK/initrd.img"
sudo chown "$(id -u):$(id -g)" "$WORK/vmlinux" "$WORK/initrd.img"
cleanup
trap - EXIT

log "installing NIKA and loading runtime images in the guest (Firecracker)"
uv run python -m nika.snapshot.base "$WORK" "$KVER"

rm -rf "$BASE.old"
[ -d "$BASE" ] && mv "$BASE" "$BASE.old"
mv "$WORK" "$BASE"
rm -rf "$BASE.old"
log "base image ready: $BASE ($(jq -r .id "$BASE/base.json" 2>/dev/null || cat "$BASE/base.json"))"
