"""WireGuard membership fault retained for the 0.1.0 ``rip_small_internet_vpn`` lab."""

from __future__ import annotations

from pydantic import BaseModel, Field

from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource

_WG_CONF = "/etc/wireguard/wg0.conf"
_WG_BACKUP = "/tmp/nika-010-wg0.conf"


class VPNMembershipMissingParams(BaseModel):
    host_name: str = Field(description="Host whose VPN peer entry is removed.")
    host_name_2: str = Field(description="VPN server host name.")


class VPNMembershipMissing(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name = "host_vpn_membership_missing"
    description = "A host is missing from the VPN server's peer membership."
    TAGS = ["vpn"]
    Params = VPNMembershipMissingParams

    def root_cause_resources(self, params: VPNMembershipMissingParams):
        return [node_resource(params.host_name_2)]

    def _peer_snippet(self, params: VPNMembershipMissingParams) -> str:
        return self.runtime.exec(
            params.host_name_2,
            f"grep -A3 '# {params.host_name}$' {_WG_CONF} 2>/dev/null || echo absent",
        ).strip()

    def _peer_commented(self, params: VPNMembershipMissingParams) -> bool:
        lines = self._peer_snippet(params).splitlines()[1:]
        return len([ln for ln in lines if ln.strip().startswith("#")]) >= 3

    def inject_fault(self, params: VPNMembershipMissingParams):
        server = params.host_name_2
        self.runtime.exec(server, f"cp {_WG_CONF} {_WG_BACKUP}")
        self.runtime.exec(
            server,
            f"sed -i '/# {params.host_name}$/{{n; s/^/# /; n; s/^/# /; n; s/^/# /;}}' "
            f"{_WG_CONF}",
        )
        self.runtime.exec(server, "wg-quick down wg0; wg-quick up wg0", timeout=20)

    def verify_fault(self, params: VPNMembershipMissingParams) -> dict:
        return build_verify_result(
            self.root_cause_name,
            self._peer_commented(params),
            {
                "vpn_server": params.host_name_2,
                "target_host": params.host_name,
                "wg_conf_snippet": self._peer_snippet(params),
            },
        )

    def recover_fault(self, params: VPNMembershipMissingParams) -> dict:
        server = params.host_name_2
        self.runtime.exec(server, f"cp {_WG_BACKUP} {_WG_CONF}")
        self.runtime.exec(server, "wg-quick down wg0; wg-quick up wg0", timeout=20)
        # The server learns a peer's endpoint only from that peer's traffic.
        self.runtime.exec(
            params.host_name, "ping -c 3 -W 1 172.16.1.1 >/dev/null 2>&1 || true"
        )
        return build_verify_result(
            self.root_cause_name,
            not self._peer_commented(params),
            {"vpn_server": server, "target_host": params.host_name},
        )
