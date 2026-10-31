"""SDN forwarding-rule failure implementations."""

from __future__ import annotations

import base64
import json

from pydantic import BaseModel, Field

from nika.problems.support.traffic import LOSS_EVIDENCE_TRAFFIC
from nika.net_env.sdn_l3_clos.fabric_manager.apply import onos_flow_snapshot
from nika.net_env.sdn_l3_clos.topology_model import (
    ONOS_OOB_IP,
    ONOS_REST_PORT,
    device_id,
    dpid_for_leaf,
    dpid_for_spine,
    rack_prefix,
)
from nika.problems.base import (
    FailureDomain,
    ProblemBase,
    build_verify_result,
)
from nika.problems.rca import node_resource
from nika.problems.support.benchmark_targets import (
    choice,
    choice_distinct,
    require_distinct_hosts,
)


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
        f"echo {payload} | base64 -d | "
        f"curl -s -u onos:rocks -H 'Content-Type: application/json' "
        f"-X POST '{url}' --data-binary @- >/dev/null || true",
        timeout=30,
    )


def _flow_rule_loop_pair(net_env, rng) -> tuple[str, str, str, str]:
    """Return (switch_a, switch_b, port_a, port_b) preferring an adjacent fabric link."""
    model = getattr(net_env, "model", None)
    if model is not None and model.leaf_spine_links:
        leaf, spine = rng.choice(model.leaf_spine_links)
        p0 = model.port_to_peer(leaf, spine)
        p1 = model.port_to_peer(spine, leaf)
        if p0 is not None and p1 is not None:
            return leaf, spine, p0.name, p1.name
    switches = net_env.ovs_switches or []
    pair = choice_distinct(rng, switches, "leaf_1")
    return pair[0], pair[1], "eth0", "eth0"


class FlowRuleShadowingParams(BaseModel):
    """Parameters for injecting a flow rule shadowing fault."""

    host_name: str = Field(description="Target OVS switch name.")


class FlowRuleShadowing(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name: str = "flow_rule_shadowing"
    description = (
        "A higher-priority flow rule on one switch matches traffic before the "
        "intended rules and silently drops it."
    )
    TAGS: str = ["sdn"]

    Params = FlowRuleShadowingParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"host_name": choice(ctx.rng, ctx.net_env.ovs_switches, ctx.host0)}

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: FlowRuleShadowingParams):
        return [node_resource(params.host_name)]

    # A priority from the fabric's own scheme (its ARP rules use 42000), above
    # the IPv4 forwarding rules (rack prefixes 30000, hosts 40000), so the rule
    # wins without standing out from operator-installed flows by priority.
    _SHADOW_PRIORITY = 42000

    def shadowed_prefix(self, params: FlowRuleShadowingParams) -> str:
        """Rack /24 the rule covers: the highest-numbered rack other than the
        switch's own, so the rule always sits on a path that reaches it."""
        model = self.net_env.model
        own = (
            model.leaf_id(params.host_name)
            if params.host_name in model.leaves
            else None
        )
        leaf_id = max(i for i in range(1, model.leaf_count + 1) if i != own)
        return rack_prefix(leaf_id)

    def _is_shadow_line(self, line: str, prefix: str) -> bool:
        return (
            f"priority={self._SHADOW_PRIORITY},ip,nw_dst={prefix} " in line
            and "actions=drop" in line
        )

    def inject_fault(self, params: FlowRuleShadowingParams):
        # Install through ONOS so the live FlowRuleProvider keeps the rule.
        # Match IPv4 to one rack prefix only: ARP, LLDP and other prefixes keep
        # using the intended lower-priority rules.
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
                "selector": {
                    "criteria": [
                        {"type": "ETH_TYPE", "ethType": "0x0800"},
                        {"type": "IPV4_DST", "ip": self.shadowed_prefix(params)},
                    ]
                },
            },
        )

    def _shadow_lines(self, params: FlowRuleShadowingParams) -> list[str]:
        flows = self.runtime.exec(
            params.host_name,
            f"ovs-ofctl -O OpenFlow13 dump-flows {params.host_name} 2>/dev/null",
        )
        prefix = self.shadowed_prefix(params)
        return [
            line.strip()
            for line in flows.splitlines()
            if self._is_shadow_line(line, prefix)
        ]

    def verify_fault(self, params: FlowRuleShadowingParams) -> dict:
        lines = self._shadow_lines(params)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=bool(lines),
            details={
                "host": params.host_name,
                "prefix": self.shadowed_prefix(params),
                "shadow_flows": lines,
            },
        )

    def recover_fault(self, params: FlowRuleShadowingParams) -> dict:
        device = _device_id_for_switch(params.host_name)
        prefix = self.shadowed_prefix(params)
        for flow in onos_flow_snapshot(self.runtime).get("flows", []):
            criteria = (flow.get("selector") or {}).get("criteria") or []
            if (
                flow.get("deviceId") == device
                and flow.get("priority") == self._SHADOW_PRIORITY
                and any(c.get("ip") == prefix for c in criteria)
            ):
                self.runtime.exec(
                    "fabric_mgr",
                    "curl -s -u onos:rocks -X DELETE "
                    f"'http://{ONOS_OOB_IP}:{ONOS_REST_PORT}/onos/v1/flows/"
                    f"{device}/{flow['id']}' >/dev/null || true",
                    timeout=30,
                )
        remaining = self._shadow_lines(params)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=not remaining,
            details={"host": params.host_name, "shadow_flows": remaining},
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
    symptom_probe = "custom"
    symptom_class = "loss"
    root_cause_name: str = "flow_rule_loop"
    TRAFFIC_PROFILE = LOSS_EVIDENCE_TRAFFIC
    description = (
        "SDN flow rules on adjacent switches send packets back and forth, so "
        "they circulate until their TTL expires."
    )
    TAGS: str = ["sdn"]

    Params = FlowRuleLoopParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        a, b, port_a, port_b = _flow_rule_loop_pair(ctx.net_env, ctx.rng)
        return {
            "host_name": a,
            "host_name_2": b,
            "port_name": port_a,
            "port_name_2": port_b,
        }

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        require_distinct_hosts(ctx, cls.root_cause_name, inject, switches=True)

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: FlowRuleLoopParams):
        left, _right = sorted((params.host_name, params.host_name_2))
        return [node_resource(left)]

    def _resolve_ports(self, params: FlowRuleLoopParams) -> tuple[str, str]:
        """Return the two ports that terminate the link between the switches.

        A given port name is kept only when the topology confirms it faces the
        other switch; otherwise the facing port is read from the fabric model,
        so both bounce rules always sit on the two ends of one link.
        """
        given = (params.port_name or "eth0", params.port_name_2 or "eth0")
        model = getattr(self.net_env, "model", None)
        if model is None:
            return given
        p0 = model.port_to_peer(params.host_name, params.host_name_2)
        p1 = model.port_to_peer(params.host_name_2, params.host_name)
        if p0 is None or p1 is None:
            return given
        return p0.name, p1.name

    # A fabric-scheme priority (see FlowRuleShadowing) above the IPv4
    # forwarding rules, so the bounce matches.
    _LOOP_PRIORITY = 42000

    def inject_fault(self, params: FlowRuleLoopParams):
        # Each end sends IPv4 arriving from the other switch straight back out
        # the same port. OpenFlow only re-emits a packet on its ingress port
        # through the reserved IN_PORT output, so a plain output:<port> would
        # be a drop. The two rules bounce traffic on the link until its TTL
        # expires; the TTL decrement keeps the loop bounded.
        port0, port1 = self._resolve_ports(params)
        for switch, port in ((params.host_name, port0), (params.host_name_2, port1)):
            device = _device_id_for_switch(switch)
            _onos_post_flow(
                self.runtime,
                device,
                {
                    "priority": self._LOOP_PRIORITY,
                    "timeout": 0,
                    "isPermanent": True,
                    "deviceId": device,
                    "treatment": {
                        "instructions": [
                            {"type": "L3MODIFICATION", "subtype": "DEC_TTL"},
                            {"type": "OUTPUT", "port": "IN_PORT"},
                        ]
                    },
                    "selector": {
                        "criteria": [
                            {
                                "type": "IN_PORT",
                                "port": _ofport(self.runtime, switch, port),
                            },
                            {"type": "ETH_TYPE", "ethType": "0x0800"},
                        ]
                    },
                },
            )

    def _bounce_installed(self, flows: str, ofport: str) -> bool:
        match = f"priority={self._LOOP_PRIORITY},ip,in_port={ofport} "
        return any(
            match in line and "IN_PORT" in line.split("actions=", 1)[-1]
            for line in flows.splitlines()
        )

    def _loop_lines(self, flows: str) -> list[str]:
        marker = f"priority={self._LOOP_PRIORITY},ip,"
        return [line.strip() for line in flows.splitlines() if marker in line]

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
        has_loop0 = self._bounce_installed(flows0, of0)
        has_loop1 = self._bounce_installed(flows1, of1)
        verified = has_loop0 and has_loop1
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host0_loop_flows": self._loop_lines(flows0),
                "host1_loop_flows": self._loop_lines(flows1),
                "port0": port0,
                "port1": port1,
                "ofport0": of0,
                "ofport1": of1,
            },
        )
