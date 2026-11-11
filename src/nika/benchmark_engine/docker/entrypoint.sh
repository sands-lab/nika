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
username="${NIKA_USER:-nika}"
if [ "${uid}" = "0" ]; then
  exec nika engine serve "$@"
fi

groupadd -o -g "${gid}" nika 2>/dev/null || true
# Kathara scopes Docker resources by passwd username as well as UID.
# A duplicate UID would make pwd.getpwuid select a different account.
existing_name="$(getent passwd "${uid}" | cut -d: -f1)"
if [ -n "${existing_name}" ] && [ "${existing_name}" != "${username}" ]; then
  echo "Engine UID ${uid} belongs to ${existing_name}, expected ${username}" >&2
  exit 1
fi
if getent passwd "${username}" >/dev/null; then
  if [ "$(id -u "${username}")" != "${uid}" ]; then
    echo "Engine username ${username} has a different UID from ${uid}" >&2
    exit 1
  fi
else
  useradd -u "${uid}" -g "${gid}" -m -d /home/nika -s /bin/sh "${username}"
fi
# Docker socket access, Containerlab's non-root group, and the sudo that
# Containerlab link faults use for tc, ip, and nsenter.
if [ -S /var/run/docker.sock ]; then
  groupadd -o -g "$(stat -c %g /var/run/docker.sock)" hostdocker 2>/dev/null || true
  usermod -aG hostdocker "${username}"
fi
groupadd clab_admins 2>/dev/null || true
usermod -aG clab_admins "${username}"
echo "${username} ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/nika

# The host's Kathara settings (device and network prefixes, network plugin),
# copied because Kathara rewrites the file.
if [ -f /run/nika/kathara.conf ]; then
  mkdir -p /home/nika/.config
  cp /run/nika/kathara.conf /home/nika/.config/kathara.conf
  chown -R "${uid}:${gid}" /home/nika/.config
fi

exec setpriv --reuid "${uid}" --regid "${gid}" --init-groups \
  env HOME=/home/nika USER="${username}" nika engine serve "$@"
