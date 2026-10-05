"""POX/OVS SDN faults retained for the 0.1.0 ``sdn_clos`` and ``sdn_star`` labs."""

from __future__ import annotations

from pydantic import BaseModel, Field

from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource

_POX_PGREP = (
    "pgrep -af pox 2>/dev/null | grep -v 'pgrep\\|bash\\|grep' | grep . || echo NONE"
)


def _pox_processes(runtime, host: str) -> str:
    return runtime.exec(host, _POX_PGREP).strip()


def _pox_running(output: str) -> bool:
    return output != "NONE" and "pox" in output


def _restart_original_pox(runtime, host: str) -> None:
    runtime.exec(host, "pkill -f pox.py || true")
    runtime.exec(
        host,
        f"nohup bash /hostlab/{host}.startup </dev/null "
        ">/tmp/nika-010-pox-restart.log 2>&1 &",
    )


class _ControllerParams(BaseModel):
    host_name: str = Field(description="Target SDN controller host name.")


class SDNControllerCrash(ProblemBase):
    failure_domain = FailureDomain.MANAGEMENT_ORCHESTRATION_PLANE
    root_cause_name = "sdn_controller_crash"
    description = "SDN controller is down."
    TAGS = ["sdn"]
    Params = _ControllerParams

    def root_cause_resources(self, params: _ControllerParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: _ControllerParams):
        self.runtime.exec(params.host_name, "pkill -f pox.py")

    def verify_fault(self, params: _ControllerParams) -> dict:
        output = _pox_processes(self.runtime, params.host_name)
        return build_verify_result(
            self.root_cause_name,
            not _pox_running(output),
            {"host": params.host_name, "pgrep_output": output},
        )

    def recover_fault(self, params: _ControllerParams) -> dict:
        _restart_original_pox(self.runtime, params.host_name)
        output = _pox_processes(self.runtime, params.host_name)
        return build_verify_result(
            self.root_cause_name,
            _pox_running(output),
            {"host": params.host_name, "pgrep_output": output},
        )


class SouthboundPortBlockParams(BaseModel):
    host_name: str = Field(description="Target SDN controller host name.")
    southbound_port: int = Field(default=6633, description="Port to block.")


class SouthboundPortBlock(ProblemBase):
    failure_domain = FailureDomain.MANAGEMENT_ORCHESTRATION_PLANE
    root_cause_name = "southbound_port_block"
    description = "Controller southbound channel port is blocked."
    TAGS = ["sdn"]
    Params = SouthboundPortBlockParams

    def root_cause_resources(self, params: SouthboundPortBlockParams):
        return [node_resource(params.host_name)]

    def _rule(self, params: SouthboundPortBlockParams) -> str:
        return f"tcp dport {params.southbound_port} drop"

    def inject_fault(self, params: SouthboundPortBlockParams):
        self.runtime.add_nft_drop_rule(params.host_name, self._rule(params))

    def verify_fault(self, params: SouthboundPortBlockParams) -> dict:
        blocked = self.runtime.nft_drop_rule_present(
            params.host_name, self._rule(params)
        )
        return build_verify_result(
            self.root_cause_name,
            blocked,
            {"host": params.host_name, "port": params.southbound_port},
        )

    def recover_fault(self, params: SouthboundPortBlockParams) -> dict:
        self.runtime.delete_nft_table(params.host_name)
        blocked = self.runtime.nft_drop_rule_present(
            params.host_name, self._rule(params)
        )
        return build_verify_result(
            self.root_cause_name,
            not blocked,
            {"host": params.host_name, "port": params.southbound_port},
        )


class SouthboundPortMismatchParams(BaseModel):
    host_name: str = Field(description="Target SDN controller host name.")
    mismatched_port: int = Field(default=6653, description="Port used after restart.")
    original_port: int = Field(
        default=6633, description="Expected original OpenFlow port."
    )


class SouthboundPortMismatch(ProblemBase):
    failure_domain = FailureDomain.MANAGEMENT_ORCHESTRATION_PLANE
    root_cause_name = "southbound_port_mismatch"
    description = "Controller southbound listen port mismatches switch config."
    TAGS = ["sdn"]
    Params = SouthboundPortMismatchParams

    def root_cause_resources(self, params: SouthboundPortMismatchParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: SouthboundPortMismatchParams):
        self.runtime.exec(params.host_name, "pkill -f pox.py")
        self.runtime.exec(
            params.host_name,
            "nohup python3 /pox/pox.py openflow.of_01 "
            f"--port={params.mismatched_port} forwarding.l2_learning "
            "</dev/null >/tmp/nika-010-pox-mismatch.log 2>&1 &",
        )

    def verify_fault(self, params: SouthboundPortMismatchParams) -> dict:
        output = _pox_processes(self.runtime, params.host_name)
        verified = _pox_running(output) and str(params.mismatched_port) in output
        return build_verify_result(
            self.root_cause_name,
            verified,
            {"host": params.host_name, "pgrep_output": output},
        )

    def recover_fault(self, params: SouthboundPortMismatchParams) -> dict:
        _restart_original_pox(self.runtime, params.host_name)
        output = _pox_processes(self.runtime, params.host_name)
        verified = _pox_running(output) and str(params.mismatched_port) not in output
        return build_verify_result(
            self.root_cause_name,
            verified,
            {"host": params.host_name, "pgrep_output": output},
        )


class FlowRuleShadowingParams(BaseModel):
    host_name: str = Field(description="Target OVS switch name.")


class FlowRuleShadowing(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name = "flow_rule_shadowing"
    description = "A higher-priority SDN rule shadows intended forwarding."
    TAGS = ["sdn"]
    Params = FlowRuleShadowingParams

    def root_cause_resources(self, params: FlowRuleShadowingParams):
        return [node_resource(params.host_name)]

    def _flows(self, switch: str) -> str:
        return self.runtime.exec(
            switch, f"ovs-ofctl dump-flows {switch} 2>/dev/null"
        ).strip()

    def inject_fault(self, params: FlowRuleShadowingParams):
        switch = params.host_name
        self.runtime.exec(
            switch, f"ovs-ofctl add-flow {switch} 'priority=100,actions=drop'"
        )

    def verify_fault(self, params: FlowRuleShadowingParams) -> dict:
        flows = self._flows(params.host_name)
        return build_verify_result(
            self.root_cause_name,
            "priority=100" in flows and "drop" in flows,
            {"host": params.host_name, "flows": flows},
        )

    def recover_fault(self, params: FlowRuleShadowingParams) -> dict:
        switch = params.host_name
        self.runtime.exec(switch, f"ovs-ofctl --strict del-flows {switch} priority=100")
        flows = self._flows(switch)
        return build_verify_result(
            self.root_cause_name,
            "priority=100" not in flows,
            {"host": switch, "flows": flows},
        )


class FlowRuleLoopParams(BaseModel):
    host_name: str = Field(description="Primary OVS switch name.")
    host_name_2: str = Field(description="Secondary OVS switch name.")


class FlowRuleLoop(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name = "flow_rule_loop"
    description = "SDN flow rules create a forwarding loop."
    TAGS = ["sdn"]
    Params = FlowRuleLoopParams

    def root_cause_resources(self, params: FlowRuleLoopParams):
        return [node_resource(params.host_name)]

    def _rules(self, params: FlowRuleLoopParams) -> dict[str, str]:
        return {params.host_name: "eth0", params.host_name_2: "eth1"}

    def _loop_state(self, params: FlowRuleLoopParams) -> dict[str, bool]:
        return {
            switch: f"in_port={port} actions="
            in self.runtime.exec(
                switch, f"ovs-ofctl --names dump-flows {switch} 2>/dev/null"
            )
            for switch, port in self._rules(params).items()
        }

    def inject_fault(self, params: FlowRuleLoopParams):
        for switch, port in self._rules(params).items():
            self.runtime.exec(
                switch,
                f"ovs-ofctl add-flow {switch} 'in_port={port},actions=output:{port}'",
            )

    def verify_fault(self, params: FlowRuleLoopParams) -> dict:
        state = self._loop_state(params)
        return build_verify_result(self.root_cause_name, all(state.values()), state)

    def recover_fault(self, params: FlowRuleLoopParams) -> dict:
        for switch, port in self._rules(params).items():
            self.runtime.exec(
                switch, f"ovs-ofctl --strict del-flows {switch} in_port={port}"
            )
        state = self._loop_state(params)
        return build_verify_result(self.root_cause_name, not any(state.values()), state)
