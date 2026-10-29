"""Keep VDE TAP ownership when an interface outlives its Docker namespace."""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
from collections.abc import Callable, Iterable

from nika.runtime.base import RuntimeCapabilityError
from nika.runtime.shared.containers import docker_client
from nika.utils.logger import log_event
from nika.utils.parallel import bounded_parallel_map


def tap_owner(lab_name: str) -> str:
    """Opaque kernel alias; the session document maps it back to its lab.

    Agents can read it with ``ip link`` on lab devices, so it names no host.
    """
    return "tap:" + hashlib.sha256(lab_name.encode()).hexdigest()


def _owner_aliases(lab_name: str) -> tuple[str, str]:
    """Current alias plus the ``nika:``-prefixed one older sessions recorded."""
    owner = tap_owner(lab_name)
    return owner, "nika:" + owner


# sysfs works in BusyBox images whose `ip` lacks -j and alias (e.g. Routinator);
# reading it works even where /sys is read-only. One exec lists TAPs and sets
# aliases; stdout reports FOREIGN (other owner) or NEED_HELPER (no netlink).
def _record_script(lab_name: str) -> str:
    owner, legacy = _owner_aliases(lab_name)
    return f"""
owner={shlex.quote(owner)}
legacy={shlex.quote(legacy)}
for dev in /sys/class/net/*; do
    [ -r "$dev/tun_flags" ] || continue
    [ $(($(cat "$dev/tun_flags") & 2)) -ne 0 ] || continue
    ifname=${{dev##*/}}
    alias=$(cat "$dev/ifalias" 2>/dev/null || true)
    if [ "$alias" = "$owner" ]; then
        continue
    fi
    if [ -n "$alias" ] && [ "$alias" != "$legacy" ]; then
        printf 'FOREIGN\\t%s\\t%s\\n' "$ifname" "$alias"
        exit 1
    fi
    if ip link set dev "$ifname" alias "$owner" >/dev/null 2>&1; then
        continue
    fi
    printf 'NEED_HELPER\\t%s\\n' "$ifname"
done
""".strip()


def _parse_record_output(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    foreign: list[tuple[str, str]] = []
    helpers: list[str] = []
    for line in text.splitlines():
        kind, sep, rest = line.partition("\t")
        if not sep:
            continue
        if kind == "FOREIGN":
            ifname, _, alias = rest.partition("\t")
            if ifname:
                foreign.append((ifname, alias))
        elif kind == "NEED_HELPER" and rest:
            helpers.append(rest)
    return foreign, helpers


def _exec_output(result) -> str:
    raw = result.output or b""
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode()
    return str(raw)


def record_container_taps(container, lab_name: str) -> None:
    """Mark only TAPs in a container owned by this lab, including replacements."""
    result = container.exec_run(["/bin/sh", "-c", _record_script(lab_name)])
    output = _exec_output(result)
    foreign, helpers = _parse_record_output(output)
    if foreign:
        ifname, alias = foreign[0]
        raise RuntimeCapabilityError(
            f"TAP {container.name}:{ifname} already has alias {alias!r}"
        )
    if result.exit_code:
        raise RuntimeCapabilityError(
            f"Cannot inspect TAPs in {container.name}: {result.output!r}"
        )
    if not helpers:
        return
    owner = tap_owner(lab_name)
    command = [
        "/bin/sh",
        "-ec",
        "\n".join(
            f"ip link set dev {shlex.quote(ifname)} alias {shlex.quote(owner)}"
            for ifname in helpers
        ),
    ]
    _run_net_admin_helper(
        command,
        f"container:{container.id}",
        lab_name,
        f"Cannot record TAP ownership in {container.name}",
    )


def record_lab_taps(
    containers: Iterable,
    lab_name: str,
    *,
    on_error: Callable[[object, BaseException], None] | None = None,
) -> None:
    """Mark TAP ownership in every running container, concurrently."""

    def record(container) -> None:
        try:
            record_container_taps(container, lab_name)
        except Exception as exc:
            if on_error is None:
                raise
            on_error(container, exc)

    bounded_parallel_map(
        record,
        [container for container in containers if container.status == "running"],
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
    aliases = _owner_aliases(lab_name)
    return [
        link
        for link in json.loads(result.stdout)
        if link.get("ifalias") in aliases
        and link.get("linkinfo", {}).get("info_data", {}).get("type") == "tap"
    ]


def cleanup_host_taps(lab_name: str) -> None:
    """Delete this lab's orphan TAPs with Docker's host-network NET_ADMIN."""
    links = owned_host_taps(lab_name)
    if not links:
        return
    # Recheck the alias and kernel identity immediately before each deletion.
    # A helper avoids adding a host-sudo requirement to Kathara deployments.
    commands = []
    for link in links:
        name = shlex.quote(link["ifname"])
        commands.append(
            f'test "$(cat /sys/class/net/{name}/ifalias)" = {shlex.quote(link["ifalias"])} '
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
