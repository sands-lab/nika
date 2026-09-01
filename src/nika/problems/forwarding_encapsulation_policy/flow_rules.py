"""SDN forwarding-rule failure implementations."""

from __future__ import annotations

import base64
import json

from pydantic import BaseModel, Field

from nika.net_env.sdn_l3_clos.topology_model import (
    ONOS_OOB_IP,
    ONOS_REST_PORT,
    device_id,
    dpid_for_leaf,
    dpid_for_spine,
)
from nika.problems.base import (
    FailureDomain,
    ProblemBase,
    build_verify_result,
)
from nika.problems.rca import node_resource


def _device_id_for_switch(switch: str) -> str:
    if switch.startswith("leaf_"):
        return device_id(dpid_for_leaf(int(switch.split("_", 1)[1])))
    if switch.startswith("spine_"):
        return device_id(dpid_for_spine(int(switch.split("_", 1)[1])))
    raise ValueError(f"unsupported SDN switch name: {switch}")


def _ofport(runtime, switch: str, port_name: str) -> str:
    raw = runtime.exec(
        switch, f"ovs-vsctl get Interface {port_name} ofport 2>/dev/null || echo -1"
    ).strip()
    if not raw.lstrip("-").isdigit() or int(raw) <= 0:
        raise ValueError(f"cannot resolve ofport for {switch}:{port_name}")
    return raw


def _onos_post_flow(runtime, device: str, body: dict) -> None:
    payload = base64.b64encode(json.dumps(body).encode()).decode()
    url = f"http://{ONOS_OOB_IP}:{ONOS_REST_PORT}/onos/v1/flows/{device}"
    runtime.exec(
        "fabric_mgr",
        f"echo {payload} | base64 -d > /tmp/nika_flow.json && "
        f"curl -s -u onos:rocks -H 'Content-Type: application/json' "
        f"-X POST '{url}' --data-binary @/tmp/nika_flow.json >/dev/null || true",
        timeout=30,
    )


class FlowRuleShadowingParams(BaseModel):
    """Parameters for injecting a flow rule shadowing fault."""

    host_name: str = Field(description="Target OVS switch name.")


class FlowRuleShadowing(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name: str = "flow_rule_shadowing"
    description = "A higher-priority SDN rule shadows intended forwarding."
    TAGS: str = ["sdn"]

    Params = FlowRuleShadowingParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: FlowRuleShadowingParams):
        return [node_resource(params.host_name)]

    # Above proactive L3 Clos fabric priorities (gateway ARP ~45k, host ~40k).
    _SHADOW_PRIORITY = 50000

    def inject_fault(self, params: FlowRuleShadowingParams):
        # Install through ONOS so the live FlowRuleProvider keeps the drop rule.
        device = _device_id_for_switch(params.host_name)
        _onos_post_flow(
            self.runtime,
            device,
            {
                "priority": self._SHADOW_PRIORITY,
                "timeout": 0,
                "isPermanent": True,
                "deviceId": device,
                "treatment": {"instructions": []},
                "selector": {"criteria": []},
            },
        )

    def verify_fault(self, params: FlowRuleShadowingParams) -> dict:
        flows = self.runtime.exec(
            params.host_name,
            f"ovs-ofctl -O OpenFlow13 dump-flows {params.host_name} 2>/dev/null",
        ).strip()
        verified = f"priority={self._SHADOW_PRIORITY}" in flows and "drop" in flows
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "flows": flows},
        )


class FlowRuleLoopParams(BaseModel):
    """Parameters for injecting a flow rule loop fault."""

    host_name: str = Field(description="Primary OVS switch name.")
    host_name_2: str = Field(description="Secondary OVS switch name.")
    port_name: str = Field(
        default="",
        description="Port on host_name facing host_name_2 (optional).",
    )
    port_name_2: str = Field(
        default="",
        description="Port on host_name_2 facing host_name (optional).",
    )


class FlowRuleLoop(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name: str = "flow_rule_loop"
    description = "SDN flow rules create a forwarding loop."
    TAGS: str = ["sdn"]

    Params = FlowRuleLoopParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: FlowRuleLoopParams):
        left, _right = sorted((params.host_name, params.host_name_2))
        return [node_resource(left)]

    def _resolve_ports(self, params: FlowRuleLoopParams) -> tuple[str, str]:
        port0 = params.port_name or "eth0"
        port1 = params.port_name_2 or "eth0"
        if params.port_name and params.port_name_2:
            return params.port_name, params.port_name_2
        model = getattr(self.net_env, "model", None)
        if model is not None:
            p0 = model.port_to_peer(params.host_name, params.host_name_2)
            p1 = model.port_to_peer(params.host_name_2, params.host_name)
            if p0 is not None and p1 is not None:
                return p0.name, p1.name
        return port0, port1

    # Above proactive L3 Clos fabric priorities so the bounce actually matches.
    _LOOP_PRIORITY = 50000

    def inject_fault(self, params: FlowRuleLoopParams):
        host0 = params.host_name
        host1 = params.host_name_2
        port0, port1 = self._resolve_ports(params)
        of0 = _ofport(self.runtime, host0, port0)
        of1 = _ofport(self.runtime, host1, port1)
        for switch, ofport in ((host0, of0), (host1, of1)):
            device = _device_id_for_switch(switch)
            _onos_post_flow(
                self.runtime,
                device,
                {
                    "priority": self._LOOP_PRIORITY,
                    "timeout": 0,
                    "isPermanent": True,
                    "deviceId": device,
                    "treatment": {"instructions": [{"type": "OUTPUT", "port": ofport}]},
                    "selector": {"criteria": [{"type": "IN_PORT", "port": ofport}]},
                },
            )

    def verify_fault(self, params: FlowRuleLoopParams) -> dict:
        host0 = params.host_name
        host1 = params.host_name_2
        port0, port1 = self._resolve_ports(params)
        of0 = _ofport(self.runtime, host0, port0)
        of1 = _ofport(self.runtime, host1, port1)
        flows0 = self.runtime.exec(
            host0,
            f"ovs-ofctl -O OpenFlow13 dump-flows {host0} 2>/dev/null",
        ).strip()
        flows1 = self.runtime.exec(
            host1,
            f"ovs-ofctl -O OpenFlow13 dump-flows {host1} 2>/dev/null",
        ).strip()
        has_loop0 = (
            f"priority={self._LOOP_PRIORITY}" in flows0
            and f"in_port={of0}" in flows0
            and f"output:{of0}" in flows0
        )
        has_loop1 = (
            f"priority={self._LOOP_PRIORITY}" in flows1
            and f"in_port={of1}" in flows1
            and f"output:{of1}" in flows1
        )
        verified = has_loop0 and has_loop1
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host0_flows": flows0,
                "host1_flows": flows1,
                "port0": port0,
                "port1": port1,
                "ofport0": of0,
                "ofport1": of1,
            },
        )
