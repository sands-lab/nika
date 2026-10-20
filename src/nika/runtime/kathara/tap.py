"""Keep VDE TAP ownership when an interface outlives its Docker namespace."""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess

from nika.runtime.base import RuntimeCapabilityError
from nika.runtime.shared.containers import docker_client
from nika.utils.logger import log_event


def tap_owner(lab_name: str) -> str:
    """Opaque kernel alias; the session document maps it back to its lab.

    Agents can read it with ``ip link`` on lab devices, so it names no host.
    """
    return "tap:" + hashlib.sha256(lab_name.encode()).hexdigest()


# sysfs works in BusyBox images whose `ip` lacks -j and alias (e.g. Routinator);
# reading it works even where /sys is read-only.
_LIST_TAPS = r"""
for dev in /sys/class/net/*; do
    [ -r "$dev/tun_flags" ] || continue
    [ $(($(cat "$dev/tun_flags") & 2)) -ne 0 ] || continue
    printf '%s\t%s\n' "${dev##*/}" "$(cat "$dev/ifalias")"
done
"""


def record_container_taps(container, lab_name: str) -> None:
    """Mark only TAPs in a container owned by this lab, including replacements."""
    result = container.exec_run(["/bin/sh", "-c", _LIST_TAPS])
    if result.exit_code:
        raise RuntimeCapabilityError(
            f"Cannot inspect TAPs in {container.name}: {result.output!r}"
        )
    owner = tap_owner(lab_name)
    for line in result.output.decode().splitlines():
        ifname, _, alias = line.partition("\t")
        if alias == owner:
            continue
        if alias:
            raise RuntimeCapabilityError(
                f"TAP {container.name}:{ifname} already has alias {alias!r}"
            )
        command = ["ip", "link", "set", "dev", ifname, "alias", owner]
        if container.exec_run(command).exit_code:
            # Unprivileged BusyBox machines can neither netlink-alias nor
            # write a read-only /sys; set it from their network namespace.
            _run_net_admin_helper(
                command,
                f"container:{container.id}",
                lab_name,
                f"Cannot record TAP ownership in {container.name}",
            )


def owned_host_taps(lab_name: str) -> list[dict]:
    """Find marked TAPs that returned to the controller's host namespace."""
    result = subprocess.run(
        ["ip", "-d", "-j", "link", "show"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    owner = tap_owner(lab_name)
    return [
        link
        for link in json.loads(result.stdout)
        if link.get("ifalias") == owner
        and link.get("linkinfo", {}).get("info_data", {}).get("type") == "tap"
    ]


def cleanup_host_taps(lab_name: str) -> None:
    """Delete this lab's orphan TAPs with Docker's host-network NET_ADMIN."""
    links = owned_host_taps(lab_name)
    if not links:
        return
    # Recheck the alias and kernel identity immediately before each deletion.
    # A helper avoids adding a host-sudo requirement to Kathara deployments.
    owner = tap_owner(lab_name)
    commands = []
    for link in links:
        name = shlex.quote(link["ifname"])
        commands.append(
            f'test "$(cat /sys/class/net/{name}/ifalias)" = {shlex.quote(owner)} '
            f'&& test "$(cat /sys/class/net/{name}/ifindex)" = {link["ifindex"]} '
            f"&& ip link delete dev {name}"
        )
    _run_net_admin_helper(
        ["/bin/sh", "-ec", "\n".join(commands)],
        "host",
        lab_name,
        f"Cannot clean TAPs for {lab_name}",
    )
    if owned_host_taps(lab_name):
        raise RuntimeCapabilityError(f"TAPs remain after closing {lab_name}")
    log_event(
        "tap_cleanup",
        f"Removed {len(links)} orphan TAP interface(s) for {lab_name}",
        lab_name=lab_name,
        count=len(links),
    )


def _run_net_admin_helper(
    command: list[str], network_mode: str, lab_name: str, error: str
) -> None:
    """Run a short-lived NET_ADMIN helper without a host-sudo requirement."""
    from nika.net_env.utils.kathara.docker_files.docker_images import (
        ensure_nika_docker_images,
        nika_image,
    )

    ensure_nika_docker_images([nika_image("base")])
    helper = docker_client().containers.create(
        nika_image("base"),
        command=command,
        network_mode=network_mode,
        cap_add=["NET_ADMIN"],
        labels={"nika.lab_name": lab_name},
    )
    try:
        helper.start()
        if helper.wait(timeout=30)["StatusCode"]:
            raise RuntimeCapabilityError(f"{error}: {helper.logs()!r}")
    finally:
        helper.remove(force=True)
