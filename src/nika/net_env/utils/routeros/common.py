"""MikroTik RouterOS (vrnetlab) image, interface naming and Kathara machine helpers.

vrnetlab boots a RouterOS CHR QEMU VM inside each container. Its data NICs map
to container interfaces ``<prefix>1`` onwards (RouterOS ``ether2``…), and
RouterOS ``ether1`` is vrnetlab's internal management port. Two layouts use it:

* A router built for RouterOS (ISP, ``routeros_simple_bgp``) reserves
  container ``eth0``, so container ``ethN`` is RouterOS ``ether{N+1}``.
* A router converted from an FRR sketch keeps the environment's ``ethN``
  names, which tasks and failures address. Each ``ethN`` is bridged to a
  ``dp{N+1}`` veth that vrnetlab attaches, so ``ethN`` is ``ether{N+2}``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from Kathara.model.Lab import Lab
from Kathara.model.Machine import Machine

from nika.net_env.utils.kathara.docker_files.docker_images import image_exists
from nika.service.lab.routeros_api import CLI_COMMAND

# MikroTik licensing blocks redistributing / auto-building this image like nika/*.
IMAGE = "vrnetlab/mikrotik_routeros:7.21.5"

# vrnetlab FTPs this file into the VM after boot. RouterOS imports a file named
# ``*.auto.rsc`` on upload and writes the import result to ``*.auto.log``.
CONFIG_FILE_PATH = "/ftpboot/config.auto.rsc"
CONFIG_LOG_COMMAND = CLI_COMMAND.format(
    command=":put [/file get config.auto.log contents]"
)
_CONFIG_APPLIED = "Script file loaded and executed successfully"

# vrnetlab attaches 31 virtio NICs, including ether1 for management.
MAX_DATA_INTERFACES = 30

# QEMU does not propagate a container interface's link state to the guest NIC.
# vrnetlab keeps its own monitor connection open, so NIKA adds a second monitor
# and a watcher that mirrors each data interface's state with ``set_link``.
# Link failures then reach RouterOS as carrier loss, as they do on FRR.
LINK_MONITOR_PORT = 4100
LINK_WATCHER_PATH = "/usr/local/bin/link-carrier"
_LINK_WATCHER = rf"""#!/bin/bash
# Mirror container interface state into RouterOS VM NIC carriers.
# Arguments: <container interface>:<vrnetlab NIC index> ...
declare -A applied
while true; do
  for pair in "$@"; do
    iface=${{pair%%:*}}; nic=${{pair##*:}}
    [ "$(cat /sys/class/net/$iface/operstate 2>/dev/null)" = down ] && link=off || link=on
    [ "${{applied[$nic]}}" = "$link" ] && continue
    # vrnetlab names the QEMU netdev of data NIC N pNN.
    printf 'set_link p%02d %s\n' "$nic" "$link" \
      | socat -t1 - TCP:127.0.0.1:{LINK_MONITOR_PORT} >/dev/null 2>&1 \
      && applied[$nic]=$link
  done
  sleep 1
done
"""
# The VM itself uses 256 MiB; the remainder covers QEMU and vrnetlab.
CONTAINER_MEMORY = "1g"
# Prefix of the veths a converted router hands to vrnetlab.
_BRIDGED_PREFIX = "dp"


def routeros_interface(name: str) -> str:
    """Translate a reserved-layout container interface to its RouterOS port."""
    if name == "lo":
        return "lo0"
    if re.fullmatch(r"eth[0-9]+", name):
        return f"ether{int(name[3:]) + 1}"
    raise ValueError(f"Unsupported RouterOS interface {name!r}")


def bridged_interface(name: str) -> str:
    """Translate a converted router's container interface to its RouterOS port."""
    if re.fullmatch(r"eth[0-9]+", name):
        return f"ether{int(name[3:]) + 2}"
    raise ValueError(f"Unsupported RouterOS interface {name!r}")


def routeros_port(net_env, node: str, name: str) -> str:
    """Return the RouterOS port behind container interface ``name`` of ``node``."""
    if node in (getattr(net_env, "native_routers", None) or {}):
        return bridged_interface(name)
    return routeros_interface(name)


def require_routeros_image(image: str = IMAGE) -> None:
    """Raise if the locally built RouterOS vrnetlab image is missing."""
    if image_exists(image):
        return
    raise RuntimeError(
        f"MikroTik RouterOS image {image!r} not found locally. Build it with:\n"
        "  ./scripts/install.sh --with-vendor-images\n"
        "That downloads CHR, clones hellt/vrnetlab into "
        ".nika_cache/vendor/vrnetlab, and tags the image. See "
        "docs/operations/network-scenarios.md#routeros-simple-bgp-scenario "
        "for the manual fallback under the same cache path."
    )


def add_routeros_router(
    lab: Lab,
    name: str,
    *,
    data_interfaces: int,
    config: str | None = None,
    cpus: float = 1.0,
    mem: str = CONTAINER_MEMORY,
) -> Machine:
    """Create a RouterOS machine and reserve its management interface.

    ``data_interfaces`` must equal the number of links attached afterwards:
    vrnetlab waits, without a timeout, for that many container interfaces.
    Connect data links after this call so they receive ``eth1`` onwards.
    Pass ``config`` here or write it later with ``set_routeros_config``.
    """
    if not 1 <= data_interfaces <= MAX_DATA_INTERFACES:
        raise ValueError(
            f"RouterOS router {name!r} needs {data_interfaces} data interfaces; "
            f"vrnetlab supports 1 to {MAX_DATA_INTERFACES}."
        )
    machine = lab.new_machine(name, image=IMAGE, cpus=cpus, mem=mem)
    _boot_meta(machine, data_interfaces)
    set_routeros_startup(
        lab, name, links=[(f"eth{i}", i) for i in range(1, data_interfaces + 1)]
    )
    if config is not None:
        set_routeros_config(machine, config)
    lab.connect_machine_to_link(name, f"{name}_reserved0", machine_iface_number=0)
    return machine


def configure_routeros_router(
    lab: Lab,
    machine: Machine,
    config: str,
    interfaces: Sequence[str],
    *,
    link_commands: Iterable[str] = (),
) -> None:
    """Make an existing machine boot RouterOS behind its unchanged ``ethN`` links.

    ``interfaces`` are the machine's container interfaces in use; each becomes
    RouterOS ``ether{N+2}``. ``link_commands`` (such as ``tc netem`` on
    ``ethN``) keep shaping the traffic that leaves through those links.
    """
    if not 1 <= len(interfaces) <= MAX_DATA_INTERFACES:
        raise ValueError(
            f"RouterOS router {machine.name!r} needs {len(interfaces)} data "
            f"interfaces; vrnetlab supports 1 to {MAX_DATA_INTERFACES}."
        )
    indices = [int(name[3:]) + 1 for name in interfaces]
    if sorted(indices) != list(range(1, len(indices) + 1)):
        # vrnetlab waits, without a timeout, for one bridged link per port and
        # takes the ports in that order, so a gap would hang the boot and shift
        # every ``ether`` name away from ``bridged_interface``.
        raise ValueError(
            f"RouterOS router {machine.name!r} must use container interfaces "
            f"eth0 to eth{len(indices) - 1}, not {sorted(interfaces)}."
        )
    machine.update_meta({"image": IMAGE, "mem": CONTAINER_MEMORY, "cpus": 1.0})
    _boot_meta(machine, len(indices))
    machine.add_meta("env", f"CLAB_INTF_PREFIX={_BRIDGED_PREFIX}")
    if machine.fs.exists("etc/frr"):
        machine.fs.removetree("etc/frr")
    set_routeros_config(machine, config)
    # vrnetlab counts every ``dp*`` link, including one standing in for the
    # management interface it expects, before it starts the VM.
    setup = [
        f"ip link add {_BRIDGED_PREFIX}0 type dummy",
        f"ip link set {_BRIDGED_PREFIX}0 up",
    ]
    for name, index in zip(interfaces, indices):
        veth, peer, bridge = f"{_BRIDGED_PREFIX}{index}", f"vp{index}", f"bp{index}"
        setup.extend(
            [
                f"ip link add {veth} type veth peer name {peer}",
                f"ip link add {bridge} type bridge",
                f"ip link set {name} master {bridge}",
                f"ip link set {peer} master {bridge}",
                f"ip link set {name} up",
                f"ip link set {peer} up",
                f"ip link set {veth} up",
                f"ip link set {bridge} up",
            ]
        )
    set_routeros_startup(
        lab,
        machine.name,
        links=list(zip(interfaces, indices)),
        setup=setup,
        link_commands=link_commands,
    )


def _boot_meta(machine: Machine, data_nics: int) -> None:
    machine.add_meta("privileged", True)
    machine.add_meta("env", f"CLAB_INTFS={data_nics}")
    # vrnetlab's default datapath expects interfaces to appear after boot
    # (Containerlab's model). Kathara attaches them before the container
    # starts; macvtap wraps the interfaces that already exist.
    machine.add_meta("args", "--connection-mode macvtap")
    machine.add_meta(
        "env",
        "QEMU_ADDITIONAL_ARGS="
        f"-monitor tcp:127.0.0.1:{LINK_MONITOR_PORT},server,nowait",
    )
    machine.create_file_from_string(_LINK_WATCHER, LINK_WATCHER_PATH)


def set_routeros_startup(
    lab: Lab,
    name: str,
    *,
    links: Sequence[tuple[str, int]],
    setup: Iterable[str] = (),
    link_commands: Iterable[str] = (),
) -> None:
    """Write the container startup: link setup, link-level commands, carrier watcher.

    ``links`` pairs each container interface with its vrnetlab NIC index.
    ``link_commands`` run in the container on its ``ethN`` devices, such as
    ``tc netem`` propagation delay; they shape the VM's traffic like a cable.
    """
    pairs = " ".join(f"{iface}:{index}" for iface, index in links)
    lab.create_file_from_list(
        [
            *setup,
            *link_commands,
            f"nohup bash {LINK_WATCHER_PATH} {pairs} >/dev/null 2>&1 &",
        ],
        f"{name}.startup",
    )


def set_routeros_config(machine: Machine, config: str) -> None:
    """Install the startup script vrnetlab uploads after the VM boots."""
    machine.create_file_from_string(config, CONFIG_FILE_PATH)


def routeros_config_check(runtime, routers: Iterable[str]) -> bool:
    """Read every router's import log; see ``routeros_config_applied``."""
    from nika.net_env.verify import exec_or_empty

    return routeros_config_applied(
        (router, exec_or_empty(runtime, router, CONFIG_LOG_COMMAND, timeout=20))
        for router in routers
    )


def with_routeros_config_check(runtime, routers: Iterable[str], result: dict) -> dict:
    """Add the startup-import check to a lab verification result."""
    applied = routeros_config_check(runtime, routers)
    result["checks"] = {"routeros_config_applied": applied, **result["checks"]}
    result["verified"] = bool(result["verified"] and applied)
    return result


def routeros_config_applied(outputs: Iterable[tuple[str, str]]) -> bool:
    """Return whether every router imported its startup configuration.

    ``outputs`` pairs each router with the output of ``CONFIG_LOG_COMMAND``.
    A missing log means the VM is still booting. A log reporting anything
    other than success is permanent, so it raises instead of waiting.
    """
    applied = True
    for router, output in outputs:
        text = output.strip()
        if _CONFIG_APPLIED in text:
            continue
        applied = False
        if text and not _config_log_pending(text):
            raise RuntimeError(
                f"RouterOS rejected the startup configuration on {router}: {text}"
            )
    return applied


def _config_log_pending(text: str) -> bool:
    # Before the upload, /file get reports a missing item; before the VM
    # accepts SSH, the exec wrapper returns no output or a timeout marker.
    return "no such item" in text or text.startswith("[TIMEOUT]")


# Menus a failure or counterexample may change, grouped so that items which
# reference each other are restored together; parents precede children.
_RESTORED_MENUS = (
    ("/ip firewall address-list",),
    ("/ip firewall filter",),
    ("/routing filter rule",),
    ("/ip route",),
    (
        "/routing ospf instance",
        "/routing ospf area",
        "/routing ospf interface-template",
    ),
    ("/routing isis instance", "/routing isis interface-template"),
    ("/routing bgp instance", "/routing bgp connection", "/routing bgp vpn"),
)


def routeros_restore_script(saved: str, current: str) -> str:
    """Return RouterOS commands that restore the menus changed since ``saved``.

    Both arguments are ``/export terse`` outputs. For each menu group whose
    static items differ, the script removes those items and re-adds the saved
    ones. Unchanged groups, addresses, interfaces and management settings are
    left alone. An empty script means nothing changed.
    """

    def lines(export: str) -> list[str]:
        return [line.rstrip() for line in export.splitlines() if line.startswith("/")]

    before, after = lines(saved), lines(current)
    if not any(line.startswith("/system identity ") for line in before):
        raise ValueError("Unrecognized RouterOS export")
    commands = []
    for menus in _RESTORED_MENUS:
        old = [
            line for line in before if line.startswith(tuple(m + " " for m in menus))
        ]
        new = [line for line in after if line.startswith(tuple(m + " " for m in menus))]
        if old != new:
            commands.extend(
                f"{menu} remove [find where !dynamic]" for menu in reversed(menus)
            )
            commands.extend(old)
    if any("'" in command for command in commands):
        raise ValueError("RouterOS export contains a single quote")
    return "; ".join(commands)
