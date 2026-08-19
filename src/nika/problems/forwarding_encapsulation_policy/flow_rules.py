"""SDN forwarding-rule failure implementations."""

from pydantic import BaseModel, Field

from nika.problems.root_cause import node_resource

from nika.problems.problem_base import (
    FailureCause,
    FailureDomain,
    FailureImpact,
    FailureScope,
    FailureSymptom,
    FailureTemporal,
    ProblemBase,
    build_verify_result,
)


class FlowRuleShadowingParams(BaseModel):
    """Parameters for injecting a flow rule shadowing fault."""

    host_name: str = Field(description="Target OVS switch name.")


class FlowRuleShadowing(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    cause = FailureCause.CONFIGURATION
    symptom = FailureSymptom.BLACKHOLE
    scope = FailureScope.PATH
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.PARTIAL
    root_cause_name: str = "flow_rule_shadowing"
    TAGS: str = ["sdn"]

    Params = FlowRuleShadowingParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: FlowRuleShadowingParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: FlowRuleShadowingParams):
        self.runtime.exec(
            params.host_name,
            f"ovs-ofctl add-flow {params.host_name} 'priority=100,actions=drop'",
        )

    def verify_fault(self, params: FlowRuleShadowingParams) -> dict:
        """Verify the OVS switch has a high-priority drop rule."""
        flows = self.runtime.exec(
            params.host_name, f"ovs-ofctl dump-flows {params.host_name} 2>/dev/null"
        ).strip()
        verified = "priority=100" in flows and "drop" in flows
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "flows": flows},
        )


class FlowRuleLoopParams(BaseModel):
    """Parameters for injecting a flow rule loop fault."""

    host_name: str = Field(description="Primary OVS switch name.")
    host_name_2: str = Field(description="Secondary OVS switch name.")


class FlowRuleLoop(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    cause = FailureCause.CONFIGURATION
    symptom = FailureSymptom.LOOP
    scope = FailureScope.PATH
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.COMPLETE
    root_cause_name: str = "flow_rule_loop"
    TAGS: str = ["sdn"]

    Params = FlowRuleLoopParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: FlowRuleLoopParams):
        left, _right = sorted((params.host_name, params.host_name_2))
        return [node_resource(left)]

    def inject_fault(self, params: FlowRuleLoopParams):
        host0 = params.host_name
        host1 = params.host_name_2
        self.runtime.exec(
            host0, f"ovs-ofctl add-flow {host0} 'in_port=eth0,actions=output:eth0'"
        )
        self.runtime.exec(
            host1, f"ovs-ofctl add-flow {host1} 'in_port=eth1,actions=output:eth1'"
        )

    def verify_fault(self, params: FlowRuleLoopParams) -> dict:
        """Verify both OVS switches have loop flow rules."""
        host0 = params.host_name
        host1 = params.host_name_2
        flows0 = self.runtime.exec(
            host0, f"ovs-ofctl dump-flows {host0} 2>/dev/null"
        ).strip()
        flows1 = self.runtime.exec(
            host1, f"ovs-ofctl dump-flows {host1} 2>/dev/null"
        ).strip()
        has_loop0 = "in_port" in flows0 and "output" in flows0
        has_loop1 = "in_port" in flows1 and "output" in flows1
        verified = has_loop0 and has_loop1
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host0_flows": flows0, "host1_flows": flows1},
        )
