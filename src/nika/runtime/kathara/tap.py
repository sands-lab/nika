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
    """Opaque kernel alias; the session document maps it back to its lab."""
    return "nika:tap:" + hashlib.sha256(lab_name.encode()).hexdigest()


def record_container_taps(container, lab_name: str) -> None:
    """Mark only TAPs in a container owned by this lab, including replacements."""
    result = container.exec_run(["ip", "-d", "-j", "link", "show"])
    if result.exit_code:
        raise RuntimeCapabilityError(f"Cannot inspect TAPs in {container.name}")
    owner = tap_owner(lab_name)
    for link in json.loads(result.output):
        if link.get("linkinfo", {}).get("info_data", {}).get("type") != "tap":
            continue
        alias = link.get("ifalias", "")
        if alias == owner:
            continue
        if alias:
            raise RuntimeCapabilityError(
                f"TAP {container.name}:{link['ifname']} already has alias {alias!r}"
            )
        result = container.exec_run(
            ["ip", "link", "set", "dev", link["ifname"], "alias", owner]
        )
        if result.exit_code:
            raise RuntimeCapabilityError(
                f"Cannot record TAP ownership in {container.name}: {result.output!r}"
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
    from nika.net_env.utils.kathara.docker_files.docker_images import (
        ensure_nika_docker_images,
        nika_image,
    )

    ensure_nika_docker_images([nika_image("base")])

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
    helper = docker_client().containers.create(
        nika_image("base"),
        command=["/bin/sh", "-ec", "\n".join(commands)],
        network_mode="host",
        cap_add=["NET_ADMIN"],
        labels={"nika.lab_name": lab_name},
    )
    try:
        helper.start()
        status = helper.wait(timeout=30)
        if status["StatusCode"]:
            raise RuntimeCapabilityError(
                f"Cannot clean TAPs for {lab_name}: {helper.logs()!r}"
            )
    finally:
        helper.remove(force=True)
    if owned_host_taps(lab_name):
        raise RuntimeCapabilityError(f"TAPs remain after closing {lab_name}")
    log_event(
        "tap_cleanup",
        f"Removed {len(links)} orphan TAP interface(s) for {lab_name}",
        lab_name=lab_name,
        count=len(links),
    )
