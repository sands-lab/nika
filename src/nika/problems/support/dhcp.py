"""ISC dhcpd helpers shared by addressing and security DHCP failures."""

from __future__ import annotations

import ipaddress
import re
import shlex
from typing import TYPE_CHECKING

from nika.problems.rca.inventory import iter_link_termination_points
from nika.problems.support.oracle import Check, OracleStep, shell

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


# Diagnosis helpers for the oracle playbooks of DHCP and host-address failures.

# Prints whether dhclient runs and the options of the client's newest lease.
DHCP_LEASE_CMD = (
    "pgrep -x dhclient >/dev/null && echo dhclient=running || echo dhclient=none; "
    "L=$(ls -t /var/lib/dhcp/dhclient*.leases 2>/dev/null | head -1); "
    'if [ -s "$L" ]; then awk \'/^lease/{b=""} {b=b $0 "\\n"} END{printf "%s", b}\' "$L" '
    "| grep -E 'fixed-address|subnet-mask|routers|domain-name-servers|server-identifier|expire'; "
    "else echo lease=none; fi"
)


def lease_option(output: str, option: str) -> str | None:
    """First value of ``option <option>`` in DHCP_LEASE_CMD output."""
    m = re.search(rf"option {re.escape(option)} ([^;]+);", output)
    return m.group(1).split(",")[0].strip() if m else None


def default_gateway(output: str) -> str | None:
    m = re.search(r"^default via (\S+)", output, re.MULTILINE)
    return m.group(1) if m else None


def first_nameserver(output: str) -> str | None:
    m = re.search(r"^nameserver\s+(\S+)", output, re.MULTILINE)
    return m.group(1) if m else None


def host_ipv4(output: str) -> ipaddress.IPv4Interface | None:
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", output)
    return ipaddress.IPv4Interface(m.group(1)) if m else None


def segment_router(net_env, host: str, intf: str = "eth0") -> str | None:
    """First router reached from *host*:*intf* through layer-2 switches."""
    routers = set(getattr(net_env, "routers", None) or [])
    switches = set(getattr(net_env, "switches", None) or [])
    links = [
        [str(ep) for ep in tps] for _k, tps in iter_link_termination_points(net_env)
    ]
    seen, frontier = {host}, [f"{host}:{intf}"]
    while frontier:
        endpoint = frontier.pop(0)
        for link in links:
            if endpoint not in link:
                continue
            for other in link:
                node = other.split(":", 1)[0]
                if node in seen:
                    continue
                seen.add(node)
                if node in routers:
                    return node
                if node in switches:
                    frontier.extend(
                        ep for lk in links for ep in lk if ep.split(":", 1)[0] == node
                    )
    return None


def addresses(output: str) -> list[ipaddress.IPv4Interface]:
    """Every ``inet a.b.c.d/n`` or ``<intf> a.b.c.d/n`` address in *output*."""
    return [
        ipaddress.IPv4Interface(m)
        for m in re.findall(r"(?:inet |^\S+ )(\d+\.\d+\.\d+\.\d+/\d+)", output, re.M)
    ]


def dhcp_client_step(
    host: str,
    command: str,
    purpose: str,
    expect: Check | None = None,
    *,
    evidence: bool = False,
    save_as: str | None = None,
    attempts: int = 1,
) -> OracleStep:
    """Run *command* on a host, then print its DHCP client state and lease."""
    return shell(
        host,
        f"{command}; {DHCP_LEASE_CMD}",
        purpose,
        expect,
        evidence=evidence,
        save_as=save_as,
        attempts=attempts,
        interval_sec=10,
    )
