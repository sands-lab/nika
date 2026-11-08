#!/bin/sh
# Start the engine service with the container's arguments as its flags
# (--host, --port, --parent-pid; the run's token is in NIKA_ENGINE_TOKEN).
# This launch interface is part of the engine API version, so the frontend
# never depends on the command that implements it. The service runs as the
# frontend's user, so the sessions and results it writes into the shared
# directories stay owned by that user.
set -eu

uid="${NIKA_UID:-0}"
gid="${NIKA_GID:-0}"
if [ "${uid}" = "0" ]; then
  exec nika engine serve "$@"
fi

groupadd -o -g "${gid}" nika 2>/dev/null || true
useradd -o -u "${uid}" -g "${gid}" -m -d /home/nika -s /bin/sh nika 2>/dev/null || true
# Docker socket access, Containerlab's non-root group, and the sudo that
# Containerlab link faults use for tc, ip, and nsenter.
if [ -S /var/run/docker.sock ]; then
  groupadd -o -g "$(stat -c %g /var/run/docker.sock)" hostdocker 2>/dev/null || true
  usermod -aG hostdocker nika
fi
groupadd clab_admins 2>/dev/null || true
usermod -aG clab_admins nika
echo "nika ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/nika

# The host's Kathara settings (device and network prefixes, network plugin),
# copied because Kathara rewrites the file.
if [ -f /run/nika/kathara.conf ]; then
  mkdir -p /home/nika/.config
  cp /run/nika/kathara.conf /home/nika/.config/kathara.conf
  chown -R "${uid}:${gid}" /home/nika/.config
fi

exec setpriv --reuid "${uid}" --regid "${gid}" --init-groups \
  env HOME=/home/nika USER=nika nika engine serve "$@"
