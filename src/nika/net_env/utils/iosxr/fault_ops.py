"""IOS XR (XRd) provider for failure operations on native routers."""

from __future__ import annotations

from nika.problems.support.router_ops import RouterFaultOps


class IosxrFaultOps(RouterFaultOps):
    profile = "iosxr"

    def bgp_asn(self) -> int:
        return self.runtime.iosxr_get_bgp_asn_number(self.node)

    def set_bgp_asn(self, asn: int) -> None:
        self.runtime.iosxr_set_bgp_asn(self.node, asn)

    def bgp_networks(self) -> list[str]:
        return self.runtime.iosxr_list_bgp_networks(self.node)

    def withdraw_bgp_network(self, prefix: str) -> None:
        self.runtime.iosxr_withdraw_bgp_prefix(self.node, prefix)
