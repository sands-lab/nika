from __future__ import annotations

import re

from pydantic import BaseModel, Field

from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.rca.inventory import (
    interface_on,
    iter_link_termination_points,
    parse_endpoint,
)
from nika.runtime.base import RuntimeCapabilityError
from nika.utils.logger import system_logger

# ==========================================
# Problem: Path MTU mismatch (undersized intermediate interface)
# ==========================================
#
# Lowers MTU on an intermediate L3 egress so DF packets larger than the path
# MTU elicit ICMP Fragmentation Needed (type 3 / code 4). Smaller DF packets
# still pass. Legacy id: link_fragmentation_disabled.


_MTU_RE = re.compile(r"\bmtu\s+(\d+)\b")

# Columns where Linux IP forwarding can emit ICMP Frag Needed.
_MTU_MISMATCH_COLUMNS = frozenset(
    {
        "dc_clos",
        "campus_lan",
        "enterprise_branch",
        "k8s_lab",
        "isp_abilene/isis",
        "isp_abilene/ospf",
        "isp_abilene/ibgp_rr",
        "isp_abilene_ebgp_rpki",
        "isp_geant_ebgp_rpki",
        "isp_abilene_ebgp_rtbh",
    }
)


class MtuMismatchParams(BaseModel):
    """Parameters for injecting a path-MTU / MTU-mismatch fault."""

    host_name: str = Field(description="Intermediate forwarding node.")
    intf_name: str = Field(description="Egress interface whose MTU is reduced.")
    mtu: int = Field(
        default=500,
        ge=68,
        le=1500,
        description="Reduced interface MTU in bytes (path MTU ceiling).",
    )


class MtuMismatch(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name: str = "mtu_mismatch"
    description = "Path MTU is misconfigured on an intermediate hop."
    TAGS: list[str] = ["link", "icmp"]
    COMPATIBLE_COLUMNS = _MTU_MISMATCH_COLUMNS

    Params = MtuMismatchParams

    symptom_desc = (
        "Users report size-dependent connectivity: small packets succeed while "
        "large DF packets fail and may report ICMP Fragmentation Needed."
    )

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.mtu = 500
        self._peer: tuple[str, str] | None = None

    def root_cause_resources(self, params: MtuMismatchParams):
        return [interface_on(self.net_env, params.host_name, params.intf_name)]

    def inject_fault(self, params: MtuMismatchParams):
        match self.lab_backend:
            case "kathara" | "containerlab":
                self._inject_mtu_mismatch(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot inject_fault: unsupported backend {backend!r}."
                )

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

    def _set_mtu(self, host: str, intf: str, mtu: int) -> None:
        self.runtime.exec(host, f"ip link set dev {intf} mtu {int(mtu)}")

    @staticmethod
    def _skip_peer_mtu(peer_host: str) -> bool:
        # Lowering MTU on k3s eth0 breaks the node dataplane so even small
        # DF pings to the controller fail (k8s_lab / llmd_lab).
        if peer_host == "controller":
            return True
        return peer_host.startswith("worker")

    def _inject_mtu_mismatch(self, params: MtuMismatchParams) -> None:
        self.mtu = params.mtu
        self._set_mtu(params.host_name, params.intf_name, params.mtu)
        peer = self._peer_endpoint(params.host_name, params.intf_name)
        self._peer = peer
        if peer is not None:
            peer_host, peer_intf = peer
            if self._skip_peer_mtu(peer_host):
                system_logger.info(
                    f"Peer MTU set skipped for k3s node {peer_host}:{peer_intf}"
                )
                self._peer = None
            else:
                try:
                    self._set_mtu(peer_host, peer_intf, params.mtu)
                except Exception as exc:  # noqa: BLE001
                    system_logger.warning(
                        f"Peer MTU set skipped for {peer_host}:{peer_intf}: {exc}"
                    )
                    self._peer = None
        system_logger.info(
            f"Injected path MTU mismatch on {params.host_name}:{params.intf_name} "
            f"(mtu={params.mtu})"
        )

    def verify_fault(self, params: MtuMismatchParams) -> dict:
        """Verify the intermediate interface MTU was reduced."""
        match self.lab_backend:
            case "kathara" | "containerlab":
                return self._verify_mtu_mismatch(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot verify_fault: unsupported backend {backend!r}."
                )

    def _read_mtu(self, host: str, intf: str) -> int | None:
        output = self.runtime.exec(
            host, f"ip -o link show dev {intf} 2>/dev/null || true"
        ).strip()
        match = _MTU_RE.search(output)
        if match is None:
            return None
        return int(match.group(1))

    def _verify_mtu_mismatch(self, params: MtuMismatchParams) -> dict:
        observed = self._read_mtu(params.host_name, params.intf_name)
        verified = observed == int(params.mtu)
        details: dict = {
            "host": params.host_name,
            "intf": params.intf_name,
            "mtu": params.mtu,
            "observed_mtu": observed,
        }
        peer = self._peer or self._peer_endpoint(params.host_name, params.intf_name)
        if peer is not None:
            peer_host, peer_intf = peer
            peer_mtu = self._read_mtu(peer_host, peer_intf)
            details["peer"] = f"{peer_host}:{peer_intf}"
            details["peer_mtu"] = peer_mtu
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details=details,
        )
