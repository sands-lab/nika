from pydantic import BaseModel, Field

from nika.problems.rca import node_resource
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.utils.logger import system_logger

logger = system_logger

# ==================================================================
# Problem: P4 switch device failure (bmv2 switch down)
# ==================================================================


# ==================================================================
# Problem: FRR service down on a router device
# ==================================================================


class FrrDownParams(BaseModel):
    """Parameters for injecting an FRR service down fault."""

    host_name: str = Field(description="Target router host name.")
    service_name: str = Field(default="frr", description="Service name.")


class FrrDown(ProblemBase):
    """FRR device down problem."""

    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    root_cause_name: str = "frr_service_down"
    description = "FRR routing daemon is unavailable."
    TAGS: str = ["frr"]

    Params = FrrDownParams

    symptom_desc = "Users report connectivity issues to other hosts in the network."

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: FrrDownParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: FrrDownParams):
        # systemctl is a no-op in Kathara; kill FRR daemons directly with pkill.
        # watchfrr must be killed first so it does not restart the routing daemons.
        for daemon in (
            "watchfrr",
            "zebra",
            "mgmtd",
            "ospfd",
            "bgpd",
            "staticd",
            "ospf6d",
            "ripd",
        ):
            self.runtime.kill_process(params.host_name, daemon)

    def verify_fault(self, params: FrrDownParams) -> dict:
        """Verify FRR is down by checking zebra is not running and routing is unavailable."""
        zebra_output = self.runtime.exec(
            params.host_name, "pgrep -a zebra 2>/dev/null || echo NONE"
        ).strip()
        # show version still succeeds in FRR 9.x when zebra is down; use show ip route instead.
        vtysh_output = self.runtime.exec(
            params.host_name, "vtysh -c 'show ip route' 2>&1 | head -3"
        ).strip()
        zebra_down = zebra_output == "NONE" or "zebra" not in zebra_output
        routing_unavailable = (
            "failed to connect" in vtysh_output.lower()
            or "not running" in vtysh_output.lower()
        )
        verified = zebra_down and routing_unavailable
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "zebra_output": zebra_output,
                "vtysh_output": vtysh_output,
                "zebra_down": zebra_down,
                "routing_unavailable": routing_unavailable,
            },
        )
