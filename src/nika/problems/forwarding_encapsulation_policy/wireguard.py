"""Site Edge WireGuard peer identity and AllowedIPs misconfigurations."""

from __future__ import annotations

import time
from ipaddress import ip_network
from typing import Any

from pydantic import BaseModel, Field

from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.rca import interface_resource
from nika.utils.logger import system_logger

# Settle after peer-key change + BGP neighbor clear so handshake/BGP state stabilize.
_SETTLE_SEC = 25
# AllowedIPs rewrite keeps BGP up; brief pause for syncconf to apply.
_ALLOWED_IPS_SETTLE_SEC = 5

# Fixed unused Curve25519 public key (not any Site Edge key in keys.txt).
WRONG_HUB_PEER_PUBLIC_KEY = "6jkI0AS2rVGsA2+bTLNGUGcQ42mmHgFZEaL9HrDpeD4="


def _spoke_site_from_edge(edge_name: str) -> str:
    if edge_name.endswith("_edge"):
        return edge_name[: -len("_edge")]
    return edge_name


def allowed_ips_for_spoke_hub_peer(
    *,
    advertised_prefixes: list[str],
    local_prefixes: list[str],
    hub_tunnel_ip: str,
    omit: str | None = None,
) -> str:
    """Build cryptokey AllowedIPs for a Branch→Hub peer.

    Keeps the Hub tunnel /32 and remote advertised enterprise prefixes, optionally
    omitting one remote business prefix (the injected misconfiguration).
    """
    local = set(local_prefixes)
    omit_norm = str(ip_network(omit, strict=False)) if omit else None
    entries = [f"{hub_tunnel_ip}/32"]
    for prefix in advertised_prefixes:
        if prefix in local:
            continue
        if omit_norm is not None and str(ip_network(prefix, strict=False)) == omit_norm:
            continue
        entries.append(prefix)
    return ", ".join(entries)


def _bgp_neighbor_established(summary: str, peer_ip: str) -> bool:
    for line in summary.splitlines():
        fields = line.split()
        if peer_ip in line and len(fields) >= 10 and fields[9].isdigit():
            return True
    return False


class WireGuardPeerKeyMisconfigParams(BaseModel):
    """Parameters for a wrong Hub peer PublicKey on a Branch Site Edge."""

    host_name: str = Field(description="Branch Site Edge router (e.g. br1_edge).")
    intf_name: str = Field(
        description="WireGuard tunnel interface toward the Hub (e.g. wg_hq)."
    )


class WireGuardPeerKeyMisconfiguration(ProblemBase):
    """Wrong WireGuard peer PublicKey on every tunnel of a Branch Edge.

    Under full WAN/hub redundancy, a single-tunnel peer-key fault would fail
    over. Inject corrupts every WireGuard peer on the selected Branch Edge so
    cross-site business for that branch fails while underlay and other branches
    stay healthy. ``intf_name`` names the primary HQ peer for root-cause scoring.
    """

    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name: str = "wireguard_peer_key_misconfiguration"
    TAGS: str = ["vpn"]
    Params = WireGuardPeerKeyMisconfigParams
    symptom_desc = (
        "A Branch Site Edge has incorrect Hub WireGuard peer public keys on all "
        "of its overlay tunnels. Provider underlay and Hub WAN endpoints remain "
        "reachable and the WireGuard interfaces stay administratively up, but "
        "no tunnel can complete a handshake, so overlay BGP and cross-site "
        "business traffic for that Branch fail while other Branches stay healthy."
    )
    supported_backends: tuple[str, ...] = ("kathara",)

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger
        self._hub_tunnel_ips: list[str] = []

    def root_cause_resources(self, params: WireGuardPeerKeyMisconfigParams):
        return [interface_resource(params.host_name, params.intf_name)]

    def _spoke_site(self, edge_name: str) -> str:
        return _spoke_site_from_edge(edge_name)

    def _spoke_tunnels(self, edge_name: str) -> list[Any]:
        spoke = self._spoke_site(edge_name)
        tunnels = getattr(self.net_env, "built_tunnels", None) or []
        return [t for t in tunnels if t.spoke == spoke]

    def _resolve_hub_tunnel_ip(self, edge: str, iface: str) -> str:
        spoke = self._spoke_site(edge)
        tunnels = getattr(self.net_env, "built_tunnels", None) or []
        for tunnel in tunnels:
            if tunnel.spoke == spoke and tunnel.spoke_iface == iface:
                return tunnel.hub_tunnel_ip
        conf = self.runtime.exec(
            edge,
            f"grep -E '^Address = ' /etc/wireguard/{iface}.conf",
        ).strip()
        if "Address = " in conf:
            cidr = conf.split("=", 1)[1].strip()
            ip = cidr.split("/", 1)[0]
            parts = ip.split(".")
            if len(parts) == 4 and parts[-1] == "2":
                return ".".join(parts[:-1] + ["1"])
        raise RuntimeError(f"Cannot resolve Hub tunnel IP for {edge}/{iface}")

    def _wg_ifaces_on_edge(self, edge: str) -> list[str]:
        tunnels = self._spoke_tunnels(edge)
        if tunnels:
            return sorted({t.spoke_iface for t in tunnels})
        # Offline / missing built_tunnels: discover conf files.
        listing = self.runtime.exec(
            edge, "ls /etc/wireguard/*.conf 2>/dev/null || true"
        ).strip()
        ifaces: list[str] = []
        for token in listing.split():
            name = token.rsplit("/", 1)[-1]
            if name.endswith(".conf"):
                ifaces.append(name[: -len(".conf")])
        return sorted(ifaces)

    def inject_fault(self, params: WireGuardPeerKeyMisconfigParams):
        edge = params.host_name
        wrong_key = WRONG_HUB_PEER_PUBLIC_KEY
        ifaces = self._wg_ifaces_on_edge(edge)
        if not ifaces:
            raise RuntimeError(f"No WireGuard interfaces found on {edge}")
        if params.intf_name not in ifaces:
            raise RuntimeError(
                f"Primary intf_name {params.intf_name!r} not among {ifaces} on {edge}"
            )

        cleared: list[str] = []
        for iface in ifaces:
            hub_tun_ip = self._resolve_hub_tunnel_ip(edge, iface)
            conf_path = f"/etc/wireguard/{iface}.conf"
            self.runtime.exec(edge, f"cp {conf_path} {conf_path}.bak")
            self.runtime.exec(
                edge,
                f"sed -i 's|^PublicKey = .*|PublicKey = {wrong_key}|' {conf_path}",
            )
            self.runtime.exec(
                edge,
                f"wg-quick strip {iface} > /tmp/{iface}.strip "
                f"&& wg syncconf {iface} /tmp/{iface}.strip",
            )
            self.runtime.exec(
                edge,
                f"vtysh -c 'clear ip bgp {hub_tun_ip}' 2>/dev/null || true",
            )
            cleared.append(hub_tun_ip)

        self._hub_tunnel_ips = cleared
        self.logger.info(
            f"Injected wrong WireGuard Hub peer PublicKey on all WG ifaces of "
            f"{edge} ({', '.join(ifaces)}); cleared BGP neighbors {cleared}."
        )
        time.sleep(_SETTLE_SEC)

    def verify_fault(self, params: WireGuardPeerKeyMisconfigParams) -> dict:
        edge = params.host_name
        expected = WRONG_HUB_PEER_PUBLIC_KEY
        ifaces = self._wg_ifaces_on_edge(edge)
        per_iface: dict[str, dict[str, Any]] = {}
        all_ok = bool(ifaces) and params.intf_name in ifaces

        for iface in ifaces:
            conf = self.runtime.exec(
                edge, f"grep -E '^PublicKey = ' /etc/wireguard/{iface}.conf || true"
            ).strip()
            conf_ok = conf == f"PublicKey = {expected}"

            link = self.runtime.exec(
                edge, f"ip -o link show {iface} 2>/dev/null || true"
            ).strip()
            iface_up = (
                bool(link) and "state DOWN" not in link and "LOWERLAYERDOWN" not in link
            )

            wg_show = self.runtime.exec(
                edge, f"wg show {iface} 2>/dev/null || true"
            ).strip()
            peer_present = expected in wg_show
            handshakes = self.runtime.exec(
                edge, f"wg show {iface} latest-handshakes 2>/dev/null || true"
            ).strip()
            handshake_ok = False
            for line in handshakes.splitlines():
                if expected not in line:
                    continue
                parts = line.split()
                if len(parts) >= 2 and parts[-1].isdigit():
                    handshake_ok = int(parts[-1]) == 0
                break

            iface_ok = conf_ok and iface_up and peer_present and handshake_ok
            all_ok = all_ok and iface_ok
            per_iface[iface] = {
                "conf_public_key_line": conf,
                "iface_link": link,
                "wg_show": wg_show,
                "latest_handshakes": handshakes,
                "conf_ok": conf_ok,
                "iface_up": iface_up,
                "peer_present": peer_present,
                "no_successful_handshake": handshake_ok,
            }

        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=all_ok,
            details={
                "host_name": edge,
                "intf_name": params.intf_name,
                "expected_public_key": expected,
                "ifaces": ifaces,
                "per_iface": per_iface,
            },
        )


class WireGuardAllowedIpsMisconfigParams(BaseModel):
    """Parameters for omitting a remote business prefix from Branch→Hub AllowedIPs."""

    host_name: str = Field(description="Branch Site Edge router (e.g. br1_edge).")
    intf_name: str = Field(
        description="WireGuard tunnel interface toward the Hub (e.g. wg_hq)."
    )
    target_prefix: str = Field(
        description=(
            "Remote advertised enterprise prefix to omit from AllowedIPs "
            "(e.g. 10.0.20.0/24)."
        )
    )


class WireGuardAllowedIpsMisconfiguration(ProblemBase):
    """Omit one remote business prefix from Site Edge Hub-peer AllowedIPs.

    Handshake and overlay BGP over the tunnel stay up and FRR still installs the
    route, but WireGuard cryptokey routing drops packets to the omitted prefix.
    """

    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name: str = "wireguard_allowed_ips_misconfiguration"
    TAGS: str = ["vpn"]
    Params = WireGuardAllowedIpsMisconfigParams
    symptom_desc = (
        "A Branch Site Edge omits one remote enterprise business prefix from its "
        "Hub WireGuard peer AllowedIPs while keeping the Hub tunnel address. "
        "The WireGuard handshake and overlay BGP session stay healthy and the "
        "prefix remains in FRR/RIB and Linux routing, but cryptokey routing "
        "blocks data traffic to that prefix. Other business prefixes, other "
        "Branches, underlay, and BGP sessions stay healthy."
    )
    supported_backends: tuple[str, ...] = ("kathara",)

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger
        self._hub_tunnel_ip: str | None = None

    def root_cause_resources(self, params: WireGuardAllowedIpsMisconfigParams):
        return [interface_resource(params.host_name, params.intf_name)]

    def _spoke_site(self, edge_name: str) -> str:
        return _spoke_site_from_edge(edge_name)

    def _resolve_hub_tunnel_ip(self, params: WireGuardAllowedIpsMisconfigParams) -> str:
        spoke = self._spoke_site(params.host_name)
        tunnels = getattr(self.net_env, "built_tunnels", None) or []
        for tunnel in tunnels:
            if tunnel.spoke == spoke and tunnel.spoke_iface == params.intf_name:
                return tunnel.hub_tunnel_ip
        conf = self.runtime.exec(
            params.host_name,
            f"grep -E '^Address = ' /etc/wireguard/{params.intf_name}.conf",
        ).strip()
        if "Address = " in conf:
            cidr = conf.split("=", 1)[1].strip()
            ip = cidr.split("/", 1)[0]
            parts = ip.split(".")
            if len(parts) == 4 and parts[-1] == "2":
                return ".".join(parts[:-1] + ["1"])
        raise RuntimeError(
            f"Cannot resolve Hub tunnel IP for {params.host_name}/{params.intf_name}"
        )

    def _local_and_advertised_prefixes(self, spoke: str) -> tuple[list[str], list[str]]:
        spec = getattr(self.net_env, "spec", None)
        if spec is None:
            raise RuntimeError(
                "enterprise_branch net_env.spec is required to build AllowedIPs"
            )
        site = spec.sites[spoke]
        local = [lan.prefix for lan in site.lans if lan.advertise]
        advertised = list(spec.advertised_prefixes())
        return local, advertised

    def inject_fault(self, params: WireGuardAllowedIpsMisconfigParams):
        iface = params.intf_name
        edge = params.host_name
        spoke = self._spoke_site(edge)
        hub_tun_ip = self._resolve_hub_tunnel_ip(params)
        self._hub_tunnel_ip = hub_tun_ip
        local, advertised = self._local_and_advertised_prefixes(spoke)
        allowlist = allowed_ips_for_spoke_hub_peer(
            advertised_prefixes=advertised,
            local_prefixes=local,
            hub_tunnel_ip=hub_tun_ip,
            omit=params.target_prefix,
        )
        if f"{hub_tun_ip}/32" not in allowlist:
            raise RuntimeError("Hub tunnel /32 missing from AllowedIPs allowlist")
        if params.target_prefix in allowlist.split(", "):
            raise RuntimeError(
                f"target_prefix {params.target_prefix} still present in allowlist"
            )

        conf_path = f"/etc/wireguard/{iface}.conf"
        self.runtime.exec(edge, f"cp {conf_path} {conf_path}.bak")
        self.runtime.exec(
            edge,
            f"sed -i 's|^AllowedIPs = .*|AllowedIPs = {allowlist}|' {conf_path}",
        )
        self.runtime.exec(
            edge,
            f"wg-quick strip {iface} > /tmp/{iface}.strip "
            f"&& wg syncconf {iface} /tmp/{iface}.strip",
        )
        self.logger.info(
            f"Injected WireGuard AllowedIPs omit of {params.target_prefix} "
            f"on {edge}/{iface} (hub tunnel {hub_tun_ip} retained; BGP untouched)."
        )
        time.sleep(_ALLOWED_IPS_SETTLE_SEC)

    def verify_fault(self, params: WireGuardAllowedIpsMisconfigParams) -> dict:
        iface = params.intf_name
        edge = params.host_name
        target = str(ip_network(params.target_prefix, strict=False))
        hub_tun_ip = self._hub_tunnel_ip or self._resolve_hub_tunnel_ip(params)
        hub_tun_cidr = f"{hub_tun_ip}/32"
        # First host in the target /24 for route lookup (LAN host is typically .2).
        target_net = ip_network(params.target_prefix, strict=False)
        target_host = str(target_net.network_address + 2)

        conf_line = self.runtime.exec(
            edge, f"grep -E '^AllowedIPs = ' /etc/wireguard/{iface}.conf || true"
        ).strip()
        allowed_value = (
            conf_line.split("=", 1)[1].strip() if "AllowedIPs = " in conf_line else ""
        )
        entries = [e.strip() for e in allowed_value.split(",") if e.strip()]
        conf_omits_target = (
            target not in entries and params.target_prefix not in entries
        )
        conf_keeps_tunnel = hub_tun_cidr in entries

        handshakes = self.runtime.exec(
            edge, f"wg show {iface} latest-handshakes 2>/dev/null || true"
        ).strip()
        handshake_ok = False
        for line in handshakes.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[-1].isdigit() and int(parts[-1]) > 0:
                handshake_ok = True
                break

        bgp_sum = self.runtime.exec(
            edge, "vtysh -c 'show bgp summary' 2>/dev/null || true"
        )
        bgp_ok = _bgp_neighbor_established(bgp_sum, hub_tun_ip)

        rib = self.runtime.exec(
            edge, f"vtysh -c 'show ip route {target}' 2>/dev/null || true"
        )
        linux_route = self.runtime.exec(
            edge, f"ip route get {target_host} 2>/dev/null || true"
        ).strip()
        route_ok = (
            target in rib or params.target_prefix in rib or target_host in linux_route
        ) and iface in linux_route

        verified = (
            conf_omits_target
            and conf_keeps_tunnel
            and handshake_ok
            and bgp_ok
            and route_ok
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host_name": edge,
                "intf_name": iface,
                "target_prefix": params.target_prefix,
                "hub_tunnel_ip": hub_tun_ip,
                "allowed_ips_line": conf_line,
                "conf_omits_target": conf_omits_target,
                "conf_keeps_tunnel": conf_keeps_tunnel,
                "latest_handshakes": handshakes,
                "handshake_ok": handshake_ok,
                "bgp_established": bgp_ok,
                "rib": rib,
                "linux_route": linux_route,
                "route_ok": route_ok,
            },
        )
