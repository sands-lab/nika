"""Restricted FRR IS-IS projection into native IOS XR configuration."""

from __future__ import annotations

import re
from ipaddress import IPv4Interface

from nika.net_env.isp.igp.frr import ISIS_PROCESS
from nika.validation.batfish.frr_ibgp import project_frr_ibgp, split_frr_ibgp


_IFACE_RE = re.compile(r"(?:lo|eth[0-9]+)\Z")
_METRIC_RE = re.compile(r"isis metric ([0-9]+)\Z")
_ADDRESS_RE = re.compile(r"ip address ([0-9.]+/[0-9]+)\Z")
_NET_RE = re.compile(
    r"(?:[0-9A-Fa-f]{2}|[0-9A-Fa-f]{4})(?:\.(?:[0-9A-Fa-f]{2}|[0-9A-Fa-f]{4}))+\Z"
)


def parse_frr_isis(node: str, config: str) -> tuple[str, dict[str, dict]]:
    """Parse the generated L2-only, wide-metric FRR IPv4 IS-IS subset.

    Interface addresses may be absent in a syntactically valid candidate so
    callers can validate withdrawal mutations against the original sketch.
    The returned metric for loopback is FRR's default IS-IS metric (10).
    """
    net = None
    router_values: dict[str, str] = {}
    interfaces: dict[str, dict] = {}
    section = "global"
    current_iface = None
    seen_global: set[str] = set()

    def reject(line: str) -> None:
        raise ValueError(f"Unsupported FRR IS-IS config on {node}: {line!r}")

    for raw in config.splitlines():
        line = raw.strip()
        if not line or line.startswith(("!", "#")):
            continue
        if line.startswith("interface "):
            name = line.removeprefix("interface ")
            if not _IFACE_RE.fullmatch(name):
                reject(line)
            current_iface = name
            interfaces.setdefault(
                name,
                {
                    "address": None,
                    "metric": 10,
                    "passive": False,
                    "isis_enabled": False,
                    "circuit_type": None,
                    "metric_explicit": False,
                    "point_to_point": False,
                },
            )
            section = "interface"
            continue
        if line == f"router isis {ISIS_PROCESS}":
            if section == "router" or "router" in seen_global:
                reject(line)
            seen_global.add("router")
            section = "router"
            current_iface = None
            continue
        if line in {
            "log file /var/log/frr/frr.log",
            "log file /var/log/frr/frr.log informational",
        }:
            if line in seen_global:
                reject(line)
            seen_global.add(line)
            continue
        if section == "global":
            if line in {
                "frr version 10.5.3",
                "frr defaults traditional",
                "log file /var/log/frr/frr.log",
                "ip forwarding",
            }:
                if line in seen_global:
                    reject(line)
                seen_global.add(line)
                continue
            if line.startswith("hostname "):
                if "hostname" in seen_global or line != f"hostname {node}":
                    reject(line)
                seen_global.add("hostname")
                continue
            reject(line)
        if section == "interface":
            iface = interfaces[current_iface]
            match = _ADDRESS_RE.fullmatch(line)
            if match:
                if iface["address"] is not None:
                    reject(line)
                try:
                    iface["address"] = IPv4Interface(match.group(1))
                except ValueError:
                    reject(line)
            elif line == f"ip router isis {ISIS_PROCESS}":
                if iface["isis_enabled"]:
                    reject(line)
                iface["isis_enabled"] = True
            elif line in {
                "isis circuit-type level-2-only",
                "isis circuit-type level-2",
            }:
                if iface["circuit_type"] is not None:
                    reject(line)
                iface["circuit_type"] = "level-2-only"
            elif line == "isis passive":
                if iface["passive"]:
                    reject(line)
                iface["passive"] = True
            elif line == "isis network point-to-point":
                if iface["point_to_point"]:
                    reject(line)
                iface["point_to_point"] = True
            elif line == "no shutdown" or line.startswith("description "):
                # Cosmetic descriptions and explicit enabled defaults do not
                # alter the modeled forwarding behavior.
                continue
            else:
                match = _METRIC_RE.fullmatch(line)
                if not match or iface["metric_explicit"]:
                    reject(line)
                metric = int(match.group(1))
                # Metric 0 occurs only on passive circuits (RouterOS advertises
                # passive prefixes with metric 0); checked below.
                if not 0 <= metric <= 16_777_214:
                    reject(line)
                iface["metric"] = metric
                iface["metric_explicit"] = True
            continue
        if section == "router":
            if line.startswith("net "):
                if net is not None:
                    reject(line)
                candidate = line.removeprefix("net ")
                if not _NET_RE.fullmatch(candidate):
                    reject(line)
                octets = bytes.fromhex(candidate.replace(".", ""))
                if len(octets) < 8 or len(octets) > 20 or octets[-1] != 0:
                    reject(line)
                net = candidate
                router_values["net"] = candidate
            elif line in {
                "is-type level-2-only",
                "is-type level-2",
                "metric-style wide",
            }:
                key, value = line.split(" ", 1)
                if key in router_values:
                    reject(line)
                router_values[key] = "level-2-only" if value == "level-2" else value
            elif line in {
                "log-adjacency-changes",
                "hostname dynamic",
                "lsp-timers gen-interval 1 refresh-interval 900 max-lifetime 1800",
            }:
                # Logging, dynamic hostnames and LSP scheduling are outside
                # the steady-state route model. Runtime checks cover execution.
                continue
            else:
                reject(line)

    if (
        net is None
        or router_values.get("is-type") != "level-2-only"
        or router_values.get("metric-style") != "wide"
    ):
        raise ValueError(f"Incomplete FRR IS-IS configuration on {node}")
    if not {"frr defaults traditional", "hostname"} <= seen_global:
        raise ValueError(f"Incomplete FRR configuration header on {node}")
    if not interfaces:
        raise ValueError(f"FRR IS-IS config on {node} has no interfaces")
    for name, iface in interfaces.items():
        if not iface["isis_enabled"]:
            if (
                iface["passive"]
                or iface["circuit_type"] is not None
                or iface["metric_explicit"]
                or iface["point_to_point"]
            ):
                raise ValueError(
                    f"Incomplete disabled FRR IS-IS interface on {node}:{name}"
                )
        elif iface["metric"] == 0 and not iface["passive"]:
            raise ValueError(f"IS-IS metric 0 requires a passive circuit on {node}")
        elif name == "lo":
            if (
                not iface["passive"]
                or iface["circuit_type"] is not None
                or (iface["metric_explicit"] and iface["metric"] != 0)
                or iface["point_to_point"]
            ):
                raise ValueError(f"Unsupported FRR IS-IS loopback config on {node}")
        elif iface["circuit_type"] != "level-2-only" or not iface["metric_explicit"]:
            raise ValueError(f"Incomplete FRR IS-IS interface config on {node}:{name}")
        iface["enabled"] = iface["isis_enabled"]
        del iface["isis_enabled"]
        del iface["circuit_type"]
        del iface["metric_explicit"]
    if "lo" not in interfaces:
        raise ValueError(f"FRR IS-IS config on {node} has no loopback")
    return net, interfaces


def project_frr_isis(
    node: str, config: str, *, shutdown=(), allow_ebgp=False, stub_addresses=()
) -> str:
    """Translate one supported FRR config to native IOS XR IS-IS syntax."""
    igp_config, bgp_fragment = split_frr_ibgp(config)
    net, interfaces = parse_frr_isis(node, igp_config)
    lines = [f"hostname {node}"]
    for original, details in interfaces.items():
        native = _native_interface(original)
        lines.append(f"interface {native}")
        if details["address"] is not None:
            lines.append(f" ipv4 address {details['address']}")
        if (node, original) in shutdown:
            lines.append(" shutdown")
        lines.append("!")
    lines.extend(
        [
            f"router isis {ISIS_PROCESS}",
            f" net {net}",
            " is-type level-2-only",
            " address-family ipv4 unicast",
            "  metric-style wide",
            " !",
        ]
    )
    for original, details in interfaces.items():
        if not details["enabled"]:
            continue
        lines.extend([f" interface {_native_interface(original)}"])
        if original != "lo":
            lines.append("  circuit-type level-2-only")
        if details["point_to_point"]:
            lines.append("  point-to-point")
        if details["passive"]:
            lines.append("  passive")
        lines.extend(
            [
                "  address-family ipv4 unicast",
                f"   metric {details['metric']} level 2",
                "  !",
                " !",
            ]
        )
    lines.append("!")
    return (
        "\n".join(lines)
        + "\n"
        + project_frr_ibgp(
            bgp_fragment, allow_ebgp=allow_ebgp, stub_addresses=stub_addresses
        )
    )


def _native_interface(original: str) -> str:
    if original == "lo":
        return "Loopback0"
    match = re.fullmatch(r"eth([0-9]+)", original)
    if match is None:
        raise ValueError(f"Unsupported FRR interface name {original!r}")
    return f"GigabitEthernet0/0/0/{match.group(1)}"
