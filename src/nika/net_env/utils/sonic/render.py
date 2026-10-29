"""Render SONiC-VS node files from an FRR router design.

A design names Linux interfaces and carries their addresses in FRR
``interface`` blocks. SONiC keeps port and address state in CONFIG_DB, which
its interface managers apply to the kernel, so the renderer moves addresses
there and keeps every routing statement in FRR under SONiC port names.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from ipaddress import ip_interface

from nika.net_env.utils.sonic.common import (
    CONFIG_DIR,
    HWSKU,
    HWSKU_DIR,
    IMAGE,
    PLATFORM,
    PORT_MTU,
    container_iface,
)

# SONiC's zebra defaults (the image's /etc/frr/zebra.conf). The FPM channel
# carries FRR routes to SONiC's APPL_DB and ASIC_DB.
_ZEBRA_DEFAULTS = (
    "no zebra nexthop kernel enable",
    "no fpm use-next-hop-groups",
    "fpm address 127.0.0.1",
    "zebra nexthop-group keep 1",
)
_DAEMONS = {"router ospf": "ospfd", "router isis": "isisd", "router bgp": "bgpd"}
# SONiC starts zebra and staticd itself; NIKA registers the protocol daemons
# the image's supervisord does not define.
_SUPERVISED = ("ospfd", "isisd")
_ADDRESS = re.compile(r"\s*ipv?6? address (\S+)")
_DROPPED = re.compile(
    r"(frr version .*|log file .*|log syslog.*|service integrated-.*)"
)


@dataclass(frozen=True)
class SonicRouter:
    """One SONiC router: its ports, loopbacks and FRR routing design."""

    name: str
    frr_conf: str
    # SONiC port -> MTU
    ports: Mapping[str, int] = field(default_factory=dict)
    # SONiC interface (port or LoopbackN) -> addresses with prefix length
    addresses: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def daemons(self) -> tuple[str, ...]:
        found = {
            daemon
            for prefix, daemon in _DAEMONS.items()
            if re.search(rf"^{prefix}\b", self.frr_conf, re.MULTILINE)
        }
        return tuple(sorted(found))


def translate_frr_design(
    name: str,
    frr_conf: str,
    rename: Callable[[str], str],
    *,
    mtu: Mapping[str, int] | None = None,
) -> SonicRouter:
    """Move a design's interface addresses to CONFIG_DB under SONiC names.

    ``rename`` maps each Linux interface the design names (``ethN``, ``lo``)
    to its SONiC interface. ``mtu`` sets port MTUs by Linux interface name.
    """
    lines = [f"hostname {name}", "log syslog informational", *_ZEBRA_DEFAULTS, "!"]
    addresses: dict[str, list[str]] = {}
    interface = None
    for line in frr_conf.splitlines():
        stripped = line.strip()
        if _DROPPED.fullmatch(stripped) or stripped.startswith("hostname "):
            continue
        if match := re.fullmatch(r"interface (\S+)", stripped):
            interface = rename(match[1])
            addresses.setdefault(interface, [])
            lines.append(f"interface {interface}")
            continue
        if not line.startswith(" "):
            interface = None
        if interface is not None and (match := _ADDRESS.fullmatch(line)):
            addresses[interface].append(str(ip_interface(match[1])))
            continue
        if match := re.fullmatch(r"(\s*neighbor \S+ update-source )(\S+)", line):
            source = match[2]
            line = match[1] + (source if _is_address(source) else rename(source))
        lines.append(line)
    ports = {iface: PORT_MTU for iface in addresses if iface.startswith("Ethernet")}
    for linux, value in (mtu or {}).items():
        ports[rename(linux)] = value
    return SonicRouter(
        name,
        "\n".join(lines).rstrip() + "\n",
        dict(sorted(ports.items(), key=lambda item: _port_index(item[0]))),
        {iface: tuple(values) for iface, values in addresses.items() if values},
    )


def _is_address(value: str) -> bool:
    try:
        ip_interface(value)
    except ValueError:
        return False
    return True


def _port_index(port: str) -> int:
    return int(port.removeprefix("Ethernet")) // 4


def _lanes(port: str) -> str:
    first = 4 * _port_index(port) + 1
    return ",".join(str(lane) for lane in range(first, first + 4))


def config_db(router: SonicRouter) -> dict:
    tables: dict[str, dict] = {
        "DEVICE_METADATA": {
            "localhost": {
                "hostname": router.name,
                "hwsku": HWSKU,
                "platform": PLATFORM,
                "type": "LeafRouter",
                # FRR keeps the routing design; bgpcfgd does not render it.
                "docker_routing_config_mode": "split",
            }
        },
        "PORT": {
            port: {
                "alias": f"fortyGigE0/{_port_index(port) * 4}",
                "index": str(_port_index(port)),
                "lanes": _lanes(port),
                "speed": "40000",
                "mtu": str(mtu),
                "admin_status": "up",
            }
            for port, mtu in router.ports.items()
        },
        "FLEX_COUNTER_TABLE": {
            "PORT": {"FLEX_COUNTER_STATUS": "enable", "POLL_INTERVAL": "10000"}
        },
    }
    for iface, addresses in router.addresses.items():
        table = "LOOPBACK_INTERFACE" if iface.startswith("Loopback") else "INTERFACE"
        rows = tables.setdefault(table, {})
        rows[iface] = {}
        rows.update({f"{iface}|{address}": {} for address in addresses})
    return tables


def _port_config(router: SonicRouter) -> str:
    rows = ["# name lanes alias index speed"]
    rows.extend(
        f"{port} {_lanes(port)} fortyGigE0/{_port_index(port) * 4} "
        f"{_port_index(port)} 40000"
        for port in router.ports
    )
    return "\n".join(rows) + "\n"


def _lanemap(router: SonicRouter) -> str:
    return "".join(f"{container_iface(port)}:{_lanes(port)}\n" for port in router.ports)


def _supervised_daemons() -> str:
    return "\n".join(
        f"[program:{daemon}]\n"
        f"command=/usr/lib/frr/{daemon} -A 127.0.0.1\n"
        "priority=14\nautostart=false\nautorestart=false\n"
        "stdout_logfile=NONE\nstdout_syslog=true\n"
        "stderr_logfile=NONE\nstderr_syslog=true\n"
        for daemon in _SUPERVISED
    )


# Runs as PID 1 before Containerlab starts supervisord.
_PREPARE = f"""#!/bin/sh
set -e
cp {CONFIG_DIR}/config_db.json /etc/sonic/config_db.json
cp {CONFIG_DIR}/port_config.ini {CONFIG_DIR}/lanemap.ini {HWSKU_DIR}/
cp {CONFIG_DIR}/frr.conf /etc/frr/frr.conf
cp {CONFIG_DIR}/frr-daemons.conf /etc/supervisor/conf.d/frr-protocols.conf
# SONiC builds its port table from the links present when start.sh runs.
[ -e /usr/bin/sonic-start.sh ] || mv /usr/bin/start.sh /usr/bin/sonic-start.sh
cp {CONFIG_DIR}/start.sh /usr/bin/start.sh
# SONiC show commands call sudo and the restricted vtysh of the BGP
# container, which this single-container image does not ship.
printf '#!/bin/sh\\nexec "$@"\\n' > /usr/local/bin/sudo
printf '#!/bin/sh\\nexec vtysh "$@"\\n' > /usr/local/bin/rvtysh
chmod +x /usr/bin/start.sh /usr/local/bin/sudo /usr/local/bin/rvtysh
# A SONiC switch routes IPv6 as well as IPv4.
sysctl -qw net.ipv6.conf.all.forwarding=1
# Management stays on its subnet; a default route through it would carry
# traffic the routing design drops.
ip route del default dev eth0 2>/dev/null || true
ip -6 route del default dev eth0 2>/dev/null || true
"""

_START = f"""#!/bin/bash
want=$(grep -c '^eth' {HWSKU_DIR}/lanemap.ini)
for _ in $(seq 1 600); do
  have=$(ls -d /sys/class/net/eth[1-9]* 2>/dev/null | wc -l)
  [ "$have" -ge "$want" ] && exec /usr/bin/sonic-start.sh
  sleep 1
done
echo "start.sh: timed out waiting for $want data links" >&2
exit 1
"""


def node_files(router: SonicRouter) -> dict[str, str]:
    """Return the files bound read-only at ``CONFIG_DIR`` in the router."""
    return {
        "config_db.json": json.dumps(config_db(router), indent=2, sort_keys=True),
        "port_config.ini": _port_config(router),
        "lanemap.ini": _lanemap(router),
        "frr.conf": router.frr_conf,
        "frr-daemons.conf": _supervised_daemons(),
        "prepare.sh": _PREPARE,
        "start.sh": _START,
    }


def clab_kind() -> dict:
    """Containerlab ``sonic-vs`` kind defaults for nodes under ``configs/<node>``."""
    return {
        "image": IMAGE,
        "binds": [f"configs/__clabNodeName__:{CONFIG_DIR}:ro"],
        # The kind runs ``/bin/bash`` with this command as PID 1, which must
        # handle Docker's stop signal itself.
        "cmd": (
            f'-c "sh {CONFIG_DIR}/prepare.sh || exit 1; '
            "trap 'exit 0' TERM INT; sleep infinity & wait $!\""
        ),
    }


def write_node_files(root, router: SonicRouter) -> None:
    directory = root / router.name
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in node_files(router).items():
        (directory / name).write_text(content, encoding="utf-8")
