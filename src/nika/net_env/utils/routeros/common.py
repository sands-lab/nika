"""MikroTik RouterOS (vrnetlab) image, interface naming and Kathara machine helpers.

vrnetlab boots a RouterOS CHR QEMU VM inside each container. It reserves the
first container interface (``eth0``, RouterOS ``ether1``) for its internal
management bridge, so data interfaces start at ``eth1`` (``ether2``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from Kathara.model.Lab import Lab
from Kathara.model.Machine import Machine

from nika.net_env.utils.kathara.docker_files.docker_images import image_exists
from nika.service.lab.routeros_api import CLI_COMMAND

BGP_PEERS_COMMAND = CLI_COMMAND.format(
    command="/routing/bgp/session/print terse where established=yes"
)

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
LINK_WATCHER_PATH = "/usr/local/bin/nika-link-carrier"
_LINK_WATCHER = rf"""#!/bin/bash
# Mirror container data-interface state into the RouterOS VM NIC carrier.
declare -A applied
while true; do
  for i in $(seq 1 "$1"); do
    [ "$(cat /sys/class/net/eth$i/operstate 2>/dev/null)" = down ] && link=off || link=on
    [ "${{applied[$i]}}" = "$link" ] && continue
    # vrnetlab names the QEMU netdev of container interface ethN pNN.
    printf 'set_link p%02d %s\n' "$i" "$link" \
      | socat -t1 - TCP:127.0.0.1:{LINK_MONITOR_PORT} >/dev/null 2>&1 \
      && applied[$i]=$link
  done
  sleep 1
done
"""
# The VM itself uses 256 MiB; the remainder covers QEMU and vrnetlab.
CONTAINER_MEMORY = "1g"


def routeros_interface(name: str) -> str:
    """Translate a container interface name to its RouterOS port name."""
    if name == "lo":
        return "lo0"
    if re.fullmatch(r"eth[0-9]+", name):
        return f"ether{int(name[3:]) + 1}"
    raise ValueError(f"Unsupported RouterOS interface {name!r}")


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
    machine.add_meta("privileged", True)
    machine.add_meta("env", f"CLAB_INTFS={data_interfaces}")
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
    lab.create_file_from_list(
        [f"nohup bash {LINK_WATCHER_PATH} {data_interfaces} >/dev/null 2>&1 &"],
        f"{name}.startup",
    )
    if config is not None:
        set_routeros_config(machine, config)
    lab.connect_machine_to_link(name, f"{name}_reserved0", machine_iface_number=0)
    return machine


def set_routeros_config(machine: Machine, config: str) -> None:
    """Install the startup script vrnetlab uploads after the VM boots."""
    machine.create_file_from_string(config, CONFIG_FILE_PATH)


def routeros_bgp_peer_query():
    """Return the RouterOS ``BgpPeerQuery`` for Established-session checks."""
    from nika.net_env.utils.routeros.parse import routeros_established_bgp_peers
    from nika.net_env.verify import BgpPeerQuery

    return BgpPeerQuery(BGP_PEERS_COMMAND, routeros_established_bgp_peers)


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
