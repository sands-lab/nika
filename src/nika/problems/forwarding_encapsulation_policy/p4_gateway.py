"""Forwarding and INT failures for the P4 gateway benchmark."""

from __future__ import annotations

from pydantic import BaseModel, Field

from nika.problems.problem_base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.root_cause import interface_resource, node_resource
from nika.problems.support.p4_gateway import (
    set_icmp_frag_needed_filter,
    set_int_mtu,
    set_silent_destination_drop,
)


class IcmpFragNeededFilterMisconfigurationParams(BaseModel):
    host_name: str = Field(default="gateway_1")


class IcmpFragNeededFilterMisconfiguration(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name = "icmp_frag_needed_filter_misconfiguration"
    symptom_desc = "The gateway filters ICMP Fragmentation Needed, creating a PMTUD black hole for bulk TCP traffic."
    TAGS = ["p4", "p4_runtime", "icmp", "http", "l4_load_balancer"]
    Params = IcmpFragNeededFilterMisconfigurationParams

    def root_cause_resources(self, params: IcmpFragNeededFilterMisconfigurationParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: IcmpFragNeededFilterMisconfigurationParams):
        self._result = set_icmp_frag_needed_filter(self.runtime, params.host_name)

    def verify_fault(self, params: IcmpFragNeededFilterMisconfigurationParams) -> dict:
        return build_verify_result(
            self.root_cause_name,
            "icmp-frag-needed" in getattr(self, "_result", ""),
            {"gateway": params.host_name},
        )


class P4TcamEntryCorruptionParams(BaseModel):
    host_name: str = Field(description="Gateway or spine with the silent drop hook.")
    target_ip: str = Field(
        description="Destination address affected by the corrupt lookup."
    )
    control_source: str = Field(description="Client used for the healthy control flow.")


class P4TcamEntryCorruption(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name = "p4_tcam_entry_corruption"
    symptom_desc = "One destination flow stops after a switch whose P4Runtime state remains healthy."
    TAGS = ["p4_runtime", "telemetry", "flow_tracking"]
    Params = P4TcamEntryCorruptionParams

    def root_cause_resources(self, params: P4TcamEntryCorruptionParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: P4TcamEntryCorruptionParams):
        self._result = set_silent_destination_drop(
            self.runtime, params.host_name, params.target_ip
        )

    def verify_fault(self, params: P4TcamEntryCorruptionParams) -> dict:
        output = getattr(self, "_result", "")
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified="Error" not in output,
            details={
                "switch": params.host_name,
                "target_ip": params.target_ip,
                "hook_programmed": "Error" not in output,
            },
        )


class IntInsufficientMtuHeadroomParams(BaseModel):
    host_name: str = Field(description="INT source gateway.")
    intf_name: str = Field(description="Gateway egress interface.")
    bmv2_port: int = Field(gt=0)
    int_mtu: int = Field(default=1480, ge=576, le=1500)


class IntInsufficientMtuHeadroom(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name = "int_insufficient_mtu_headroom"
    symptom_desc = "Near-MTU watched packets cannot carry the fixed INT-MX header."
    TAGS = ["p4_runtime", "int", "telemetry", "http"]
    Params = IntInsufficientMtuHeadroomParams

    def root_cause_resources(self, params: IntInsufficientMtuHeadroomParams):
        return [interface_resource(params.host_name, params.intf_name)]

    def inject_fault(self, params: IntInsufficientMtuHeadroomParams):
        self._result = set_int_mtu(
            self.runtime, params.host_name, params.bmv2_port, params.int_mtu
        )

    def verify_fault(self, params: IntInsufficientMtuHeadroomParams) -> dict:
        output = getattr(self, "_result", "")
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=str(params.int_mtu) in output,
            details={"interface": params.intf_name, "int_mtu": params.int_mtu},
        )
