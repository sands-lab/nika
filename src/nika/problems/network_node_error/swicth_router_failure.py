from pydantic import BaseModel, Field

from nika.problems.root_cause import node_resource
from nika.problems.problem_base import (
    RootCauseCategory,
    build_verify_result,
    ProblemBase,
)
from nika.utils.logger import system_logger

logger = system_logger

# ==================================================================
# Problem: P4 switch device failure (bmv2 switch down)
# ==================================================================


class Bmv2SwitchDownParams(BaseModel):
    """Parameters for injecting a BMv2 switch down fault."""

    host_name: str = Field(description="Target BMv2 switch name.")


class Bmv2SwitchDown(ProblemBase):
    root_cause_category = RootCauseCategory.LINK_FAILURE
    root_cause_name = "bmv2_switch_down"
    TAGS: str = ["p4"]

    Params = Bmv2SwitchDownParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: Bmv2SwitchDownParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: Bmv2SwitchDownParams):
        self.runtime.exec(params.host_name, "pkill simple_switch")

    def verify_fault(self, params: Bmv2SwitchDownParams) -> dict:
        """Verify simple_switch process is NOT running on the BMv2 switch."""
        pgrep_output = self.runtime.exec(
            params.host_name, "pgrep -a simple_switch 2>/dev/null || echo NONE"
        ).strip()
        verified = pgrep_output == "NONE" or "simple_switch" not in pgrep_output
        return build_verify_result(
            root_cause_name=self.root_cause_name,
            faulty_devices=self.faulty_devices,
            verified=verified,
            details={"host": params.host_name, "pgrep_output": pgrep_output},
        )


# ==================================================================
# Problem: FRR service down on a router device
# ==================================================================


class FrrDownParams(BaseModel):
    """Parameters for injecting an FRR service down fault."""

    host_name: str = Field(description="Target router host name.")
    service_name: str = Field(default="frr", description="Service name.")


class FrrDown(ProblemBase):
    """FRR device down problem."""

    root_cause_category: RootCauseCategory = RootCauseCategory.NETWORK_NODE_ERROR
    root_cause_name: str = "frr_service_down"
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
            root_cause_name=self.root_cause_name,
            faulty_devices=self.faulty_devices,
            verified=verified,
            details={
                "host": params.host_name,
                "zebra_output": zebra_output,
                "vtysh_output": vtysh_output,
                "zebra_down": zebra_down,
                "routing_unavailable": routing_unavailable,
            },
        )
