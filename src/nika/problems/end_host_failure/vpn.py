from pydantic import BaseModel, Field

from nika.problems.root_cause import node_resource
from nika.problems.problem_base import (
    RootCauseCategory,
    build_verify_result,
    ProblemBase,
)
from nika.utils.logger import system_logger

# ==========================================
# Problem: VPN membership missing on end host causing inability to access services over VPN.
# ==========================================


class VPNMembershipMissingParams(BaseModel):
    """Parameters for injecting a VPN membership missing fault."""

    host_name: str = Field(description="Target host to remove from VPN.")
    host_name_2: str = Field(description="VPN server host name.")


class VPNMembershipMissing(ProblemBase):
    root_cause_category: RootCauseCategory = RootCauseCategory.END_HOST_FAILURE
    root_cause_name: str = "host_vpn_membership_missing"
    TAGS: str = ["vpn"]

    Params = VPNMembershipMissingParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: VPNMembershipMissingParams):
        return [node_resource(params.host_name_2)]

    def inject_fault(self, params: VPNMembershipMissingParams):
        target_host = params.host_name
        vpn_server = params.host_name_2

        self.runtime.exec(
            vpn_server,
            "cp /etc/wireguard/wg0.conf /etc/wireguard/wg0.conf.bak",
        )
        self.runtime.exec(
            vpn_server,
            f"sed -i '/# {target_host}/{{n; s/^/# /; n; s/^/# /; n; s/^/# /;}}' /etc/wireguard/wg0.conf",
        )
        self.runtime.exec(
            vpn_server,
            "wg-quick down wg0 && wg-quick up wg0",
        )
        self.logger.info(f"Removed VPN membership of {target_host} on {vpn_server}.")

    def verify_fault(self, params: VPNMembershipMissingParams) -> dict:
        """Verify the VPN config for target_host has commented-out lines."""
        target_host = params.host_name
        vpn_server = params.host_name_2
        wg_conf_snippet = self.runtime.exec(
            vpn_server,
            f"grep -A4 '# {target_host}' /etc/wireguard/wg0.conf 2>/dev/null || echo absent",
        ).strip()
        lines = wg_conf_snippet.splitlines()
        commented_lines = [ln for ln in lines if ln.strip().startswith("#")]
        verified = len(commented_lines) >= 3
        return build_verify_result(
            root_cause_name=self.root_cause_name,
            faulty_devices=self.faulty_devices,
            verified=verified,
            details={
                "vpn_server": vpn_server,
                "target_host": target_host,
                "wg_conf_snippet": wg_conf_snippet,
            },
        )
