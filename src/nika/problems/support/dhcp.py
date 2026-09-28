"""ISC dhcpd helpers shared by addressing and security DHCP failures."""

from __future__ import annotations

import ipaddress
import re
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nika.runtime.base import LabRuntime

DHCPD_CONF = "/etc/dhcp/dhcpd.conf"


def client_subnet(
    runtime: "LabRuntime", client_host: str, dhcp_server: str | None = None
) -> str:
    """Return the client's IPv4 network address for dhcpd subnet matching.

    Falls back to the first ``subnet`` declaration on ``dhcp_server`` when the
    client currently has no address (for example after its lease expired).
    """
    ip = runtime.get_host_ip(client_host, with_prefix=True)
    if not ip:
        for intf in ("eth0", "eth1"):
            line = runtime.exec(
                client_host,
                f"ip -4 -o addr show dev {intf} scope global 2>/dev/null | head -1",
            ).strip()
            match = re.search(r"inet\s+(\S+)", line)
            if match:
                ip = match.group(1)
                break
    if not ip and dhcp_server:
        conf = runtime.exec(
            dhcp_server,
            f"awk '/^subnet /{{print $2; exit}}' {DHCPD_CONF} 2>/dev/null || true",
        ).strip()
        if conf:
            return str(ipaddress.ip_address(conf))
    if not ip:
        raise ValueError(f"No IPv4 address on DHCP client {client_host}")
    return str(ipaddress.ip_network(ip, strict=False).network_address)


def _subnet_header(subnet: str) -> str:
    """sed/awk regex for the ``subnet <net> netmask`` declaration line."""
    return rf"^[[:space:]]*subnet {re.escape(subnet)} netmask "


def set_subnet_option(
    runtime: "LabRuntime", dhcp_server: str, subnet: str, option: str, value: str
) -> None:
    """Set ``option <option> <value>;`` inside one subnet declaration and restart."""
    header = _subnet_header(subnet)
    line = f"    option {option} {value};"
    runtime.exec(
        dhcp_server,
        f"sed -i -e '/{header}/,/}}/{{/option {option} /d}}' "
        f"-e '/{header}/a\\{line}' {DHCPD_CONF}",
    )
    runtime.systemctl(dhcp_server, "isc-dhcp-server", "restart")


def subnet_option_value(
    runtime: "LabRuntime", dhcp_server: str, subnet: str, option: str
) -> str | None:
    """Return the live value of ``option`` in one subnet declaration, if set."""
    header = _subnet_header(subnet)
    script = (
        f"$0 ~ /{header}/ {{inside=1}} "
        f'inside && $1 == "option" && $2 == "{option}" '
        '{sub(/;.*/, "", $3); print $3; exit} '
        "inside && /}/ {inside=0}"
    )
    raw = runtime.exec(
        dhcp_server, f"awk {shlex.quote(script)} {DHCPD_CONF} 2>/dev/null || true"
    ).strip()
    return raw or None


def subnet_declared(runtime: "LabRuntime", dhcp_server: str, subnet: str) -> int:
    """Return how many declarations for ``subnet`` exist (-1 when unreadable)."""
    raw = runtime.exec(
        dhcp_server,
        f"grep -cE '{_subnet_header(subnet)}' {DHCPD_CONF} 2>/dev/null || true",
    ).strip()
    try:
        return int(raw.splitlines()[-1])
    except (IndexError, ValueError):
        return -1
