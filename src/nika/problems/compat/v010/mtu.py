"""0.1.0 ``mtu_mismatch``: lowers both ends of the link and labels both."""

from __future__ import annotations

from nika.problems.base import build_verify_result
from nika.problems.forwarding_encapsulation_policy.mtu import (
    MtuMismatch,
    MtuMismatchParams,
)
from nika.problems.rca.inventory import (
    interface_on,
    iter_link_termination_points,
    parse_endpoint,
)
from nika.utils.logger import system_logger


class MtuMismatchTwoSided(MtuMismatch):
    description = "Path MTU is misconfigured on an intermediate hop."
    symptom_desc = (
        "Users report size-dependent connectivity: small packets succeed while "
        "large DF packets fail and may report ICMP Fragmentation Needed."
    )

    def root_cause_resources(self, params: MtuMismatchParams):
        resources = [interface_on(self.net_env, params.host_name, params.intf_name)]
        peer = self._mtu_peer(params)
        if peer is not None:
            resources.append(interface_on(self.net_env, *peer))
        return resources

    def _mtu_peer(self, params: MtuMismatchParams) -> tuple[str, str] | None:
        """Point-to-point peer whose MTU inject also lowers (None when skipped)."""
        peer = self._peer_endpoint(params.host_name, params.intf_name)
        if peer is None or self._skip_peer_mtu(peer[0]):
            return None
        return peer

    def _peer_endpoint(self, host: str, intf: str) -> tuple[str, str] | None:
        needle = f"{host}:{intf}"
        for _key, tps in iter_link_termination_points(self.net_env):
            endpoints = [str(ep) for ep in tps]
            if needle not in endpoints or len(endpoints) != 2:
                continue
            other = endpoints[0] if endpoints[1] == needle else endpoints[1]
            peer_host, peer_intf = parse_endpoint(other)
            if peer_host and peer_intf:
                return peer_host, peer_intf
        return None

    @staticmethod
    def _skip_peer_mtu(peer_host: str) -> bool:
        # Lowering MTU on k3s eth0 breaks the node dataplane so even small
        # DF pings to the controller fail (k8s_lab / llmd_lab).
        return peer_host == "controller" or peer_host.startswith("worker")

    def _inject_mtu_mismatch(self, params: MtuMismatchParams) -> None:
        super()._inject_mtu_mismatch(params)
        # Ground truth labels this peer too, so a failed peer write must fail inject.
        peer = self._mtu_peer(params)
        if peer is not None:
            self._set_mtu(peer[0], peer[1], params.mtu)
            system_logger.info(f"Injected MTU mismatch on peer {peer[0]}:{peer[1]}")

    def _verify_mtu_mismatch(self, params: MtuMismatchParams) -> dict:
        result = super()._verify_mtu_mismatch(params)
        peer = self._mtu_peer(params)
        if peer is None:
            return result
        peer_mtu = self._read_mtu(*peer)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=result["verified"] and peer_mtu == int(params.mtu),
            details={
                **result["details"],
                "peer": f"{peer[0]}:{peer[1]}",
                "peer_mtu": peer_mtu,
            },
        )
