"""Site Edge WireGuard peer identity misconfigurations."""

from __future__ import annotations

import time

from pydantic import BaseModel, Field

from nika.problems.problem_base import (
    RootCauseCategory,
    build_verify_result,
    ProblemBase,
)
from nika.problems.root_cause import interface_resource
from nika.utils.logger import system_logger

# Settle after peer-key change + BGP neighbor clear so handshake/BGP state stabilize.
_SETTLE_SEC = 25

# Fixed unused Curve25519 public key (not any Site Edge key in keys.txt).
WRONG_HUB_PEER_PUBLIC_KEY = "6jkI0AS2rVGsA2+bTLNGUGcQ42mmHgFZEaL9HrDpeD4="


class WireGuardPeerKeyMisconfigParams(BaseModel):
    """Parameters for a wrong Hub peer PublicKey on a Branch Site Edge."""

    host_name: str = Field(description="Branch Site Edge router (e.g. br1_edge).")
    intf_name: str = Field(
        description="WireGuard tunnel interface toward the Hub (e.g. wg_hq)."
    )


class WireGuardPeerKeyMisconfiguration(ProblemBase):
    """Wrong WireGuard peer PublicKey on a Branch Edge toward its Hub.

    Underlay WAN reachability and the WG interface stay up, but peer
    authentication fails so the tunnel never completes a handshake and overlay
    BGP over that tunnel cannot establish.
    """

    root_cause_category: RootCauseCategory = RootCauseCategory.MISCONFIGURATION
    root_cause_name: str = "wireguard_peer_key_misconfiguration"
    TAGS: str = ["vpn"]
    Params = WireGuardPeerKeyMisconfigParams
    symptom_desc = (
        "A Branch Site Edge has an incorrect Hub WireGuard peer public key. "
        "Provider underlay and the Hub WAN endpoint remain reachable and the "
        "WireGuard interface stays administratively up, but the tunnel cannot "
        "complete a handshake, so overlay BGP and cross-site business traffic "
        "for that Branch fail while other Branches stay healthy."
    )
    supported_backends: tuple[str, ...] = ("kathara",)

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger
        self._hub_tunnel_ip: str | None = None

    def root_cause_resources(self, params: WireGuardPeerKeyMisconfigParams):
        return [interface_resource(params.host_name, params.intf_name)]

    def _spoke_site(self, edge_name: str) -> str:
        if edge_name.endswith("_edge"):
            return edge_name[: -len("_edge")]
        return edge_name

    def _resolve_hub_tunnel_ip(self, params: WireGuardPeerKeyMisconfigParams) -> str:
        spoke = self._spoke_site(params.host_name)
        tunnels = getattr(self.net_env, "built_tunnels", None) or []
        for tunnel in tunnels:
            if tunnel.spoke == spoke and tunnel.spoke_iface == params.intf_name:
                return tunnel.hub_tunnel_ip
        # Offline / missing built_tunnels: read peer address from the WG conf.
        conf = self.runtime.exec(
            params.host_name,
            f"grep -E '^Address = ' /etc/wireguard/{params.intf_name}.conf",
        ).strip()
        # Address = 172.30.x.y/30 on spoke (.2); hub is .1 of the same /30.
        if "Address = " in conf:
            cidr = conf.split("=", 1)[1].strip()
            ip = cidr.split("/", 1)[0]
            parts = ip.split(".")
            if len(parts) == 4 and parts[-1] == "2":
                return ".".join(parts[:-1] + ["1"])
        raise RuntimeError(
            f"Cannot resolve Hub tunnel IP for {params.host_name}/{params.intf_name}"
        )

    def inject_fault(self, params: WireGuardPeerKeyMisconfigParams):
        iface = params.intf_name
        edge = params.host_name
        wrong_key = WRONG_HUB_PEER_PUBLIC_KEY
        hub_tun_ip = self._resolve_hub_tunnel_ip(params)
        self._hub_tunnel_ip = hub_tun_ip

        conf_path = f"/etc/wireguard/{iface}.conf"
        self.runtime.exec(edge, f"cp {conf_path} {conf_path}.bak")
        # Use '|' so base64 key characters (+ / =) do not break sed.
        self.runtime.exec(
            edge,
            f"sed -i 's|^PublicKey = .*|PublicKey = {wrong_key}|' {conf_path}",
        )
        # Reload peer config without taking the interface administratively down.
        self.runtime.exec(
            edge,
            f"wg-quick strip {iface} > /tmp/{iface}.strip "
            f"&& wg syncconf {iface} /tmp/{iface}.strip",
        )
        # Session clear only — does not change FRR BGP configuration.
        self.runtime.exec(
            edge,
            f"vtysh -c 'clear ip bgp {hub_tun_ip}' 2>/dev/null || true",
        )
        self.logger.info(
            f"Injected wrong WireGuard Hub peer PublicKey on {edge}/{iface}; "
            f"cleared BGP neighbor {hub_tun_ip}."
        )
        time.sleep(_SETTLE_SEC)

    def verify_fault(self, params: WireGuardPeerKeyMisconfigParams) -> dict:
        iface = params.intf_name
        edge = params.host_name
        expected = WRONG_HUB_PEER_PUBLIC_KEY

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
            # "<pubkey>\\t<unix_ts>"; 0 means never completed a handshake.
            parts = line.split()
            if len(parts) >= 2 and parts[-1].isdigit():
                handshake_ok = int(parts[-1]) == 0
            break

        verified = conf_ok and iface_up and peer_present and handshake_ok
        return build_verify_result(
            root_cause_name=self.root_cause_name,
            faulty_devices=self.faulty_devices,
            verified=verified,
            details={
                "host_name": edge,
                "intf_name": iface,
                "expected_public_key": expected,
                "conf_public_key_line": conf,
                "iface_link": link,
                "wg_show": wg_show,
                "latest_handshakes": handshakes,
                "conf_ok": conf_ok,
                "iface_up": iface_up,
                "peer_present": peer_present,
                "no_successful_handshake": handshake_ok,
            },
        )
