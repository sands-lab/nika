"""Evidence-backed gateway and NAT resource failures."""

from __future__ import annotations

import time

from pydantic import BaseModel, Field

from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource
from nika.problems.support.router_ops import native_fault_ops


class LbPendingConnectionUpdateRaceParams(BaseModel):
    host_name: str = Field(default="gateway_1")
    learning_delay_ms: int = Field(default=8000, ge=1, le=60000)
    seed: int = Field(default=1)


class LbPendingConnectionUpdateRace(ProblemBase):
    failure_domain = FailureDomain.SERVICE_NETWORKING
    symptom_probe = "custom"
    symptom_class = "gray"
    root_cause_name = "lb_pending_connection_update_race"
    description = "Pending connection races an unsafe load-balancer pool update."
    symptom_desc = (
        "Connection learning lags, so connections opened shortly before a routine "
        "DIP pool update are still pending in ConnTable and move to the new pool."
    )
    TAGS = ["p4", "p4_runtime", "http", "l4_load_balancer"]
    Params = LbPendingConnectionUpdateRaceParams

    BENCHMARK_TARGETS = "canonical"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        if ctx.scenario != "p4_dc_gateway":
            return {"host_name": ctx.host0}
        return {
            "host_name": "gateway_1",
            "learning_delay_ms": "8000",
            "seed": str(ctx.seed),
        }

    def root_cause_resources(self, params: LbPendingConnectionUpdateRaceParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: LbPendingConnectionUpdateRaceParams):
        from nika.net_env.p4_dc_gateway.l4_lb import set_learning_delay

        set_learning_delay(self.runtime, params.host_name, params.learning_delay_ms)
        # The agent rereads its configuration once a second.
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.verify_fault(params)["verified"]:
                return
            time.sleep(0.5)

    def verify_fault(self, params: LbPendingConnectionUpdateRaceParams) -> dict:
        from nika.net_env.p4_dc_gateway.l4_lb import learner_status
        from nika.net_env.p4_dc_gateway.topology_model import (
            CONNECTION_LEARNING_DELAY_MS,
        )

        status = learner_status(self.runtime, params.host_name)
        delay = status.get("learning_delay_ms")
        return build_verify_result(
            self.root_cause_name,
            isinstance(delay, int)
            and delay == params.learning_delay_ms
            and delay > CONNECTION_LEARNING_DELAY_MS
            and status.get("age_sec", 99) < 5,
            {
                "gateway": params.host_name,
                "healthy_learning_delay_ms": CONNECTION_LEARNING_DELAY_MS,
                "learner": status,
            },
        )


class SnatPortPoolExhaustionParams(BaseModel):
    host_name: str = Field(default="br1_edge")
    source_prefix: str = Field(default="10.1.40.0/24")
    public_ip: str
    port_start: int = Field(default=40000, ge=1024, le=65535)
    port_end: int = Field(default=40063, ge=1024, le=65535)


class SnatPortPoolExhaustion(ProblemBase):
    failure_domain = FailureDomain.SERVICE_NETWORKING
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name = "snat_port_pool_exhaustion"
    description = "SNAT source-port pool is exhausted."
    symptom_desc = "The available SNAT source-port pool is exhausted for concurrent outbound connections."
    TAGS = ["vpn", "http", "nat"]
    native_router_operations = frozenset({"add_source_nat", "source_nat_rules"})
    Params = SnatPortPoolExhaustionParams

    BENCHMARK_TARGETS = "canonical"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        if ctx.scenario != "enterprise_branch":
            return {"host_name": ctx.host0}
        return {
            "host_name": "br1_edge",
            "source_prefix": "10.1.40.0/24",
            "public_ip": "198.18.1.10",
            "port_start": "40000",
            "port_end": "40063",
        }

    def root_cause_resources(self, params: SnatPortPoolExhaustionParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: SnatPortPoolExhaustionParams):
        if params.port_end < params.port_start:
            raise ValueError("port_end must be greater than or equal to port_start")
        ops = native_fault_ops(self, params.host_name, "add_source_nat")
        if ops is not None:
            # Ahead of the healthy pool rule, like the higher-priority nft chain.
            ops.add_source_nat(
                params.source_prefix,
                params.public_ip,
                protocol="tcp",
                to_ports=f"{params.port_start}-{params.port_end}",
            )
            return
        self.runtime.exec(
            params.host_name,
            "nft add chain ip nat snat_outbound "
            "'{ type nat hook postrouting priority 99 ; policy accept; }' 2>/dev/null || true",
        )
        self.runtime.exec(params.host_name, "nft flush chain ip nat snat_outbound")
        self.runtime.exec(
            params.host_name,
            f"nft add rule ip nat snat_outbound ip protocol tcp "
            f"ip saddr {params.source_prefix} "
            f"snat to {params.public_ip}:{params.port_start}-{params.port_end}",
        )

    def verify_fault(self, params: SnatPortPoolExhaustionParams) -> dict:
        ops = native_fault_ops(self, params.host_name, "source_nat_rules")
        if ops is not None:
            ports = f"{params.port_start}-{params.port_end}"
            rules = ops.source_nat_rules()
            return build_verify_result(
                self.root_cause_name,
                any(
                    rule.source_prefix == params.source_prefix
                    and rule.to_address == params.public_ip
                    and rule.to_ports == ports
                    for rule in rules
                ),
                {"edge": params.host_name, "rules": [vars(rule) for rule in rules]},
            )
        rules = self.runtime.exec(params.host_name, "nft -a list table ip nat")
        wanted = f"{params.public_ip}:{params.port_start}-{params.port_end}"
        return build_verify_result(
            self.root_cause_name,
            wanted in rules,
            {"edge": params.host_name, "rules": rules},
        )


class NatMappingRemovedWithoutDrainParams(BaseModel):
    host_name: str = Field(default="br1_edge")
    source_prefix: str = Field(default="10.1.40.0/24")
    nat_ip_a: str
    nat_ip_b: str
    wan_interface: str = Field(
        description="Edge interface that owns the SNAT addresses."
    )


class NatMappingRemovedWithoutDrain(ProblemBase):
    failure_domain = FailureDomain.SERVICE_NETWORKING
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name = "nat_mapping_removed_without_drain"
    description = "NAT mapping is removed before active flows drain."
    symptom_desc = (
        "An active SNAT address is removed without draining its conntrack mappings."
    )
    TAGS = ["vpn", "http", "nat"]
    native_router_operations = frozenset(
        {
            "add_source_nat",
            "clear_nat_mappings",
            "interface_addresses",
            "nat_mapping_count",
            "remove_interface_address",
        }
    )
    Params = NatMappingRemovedWithoutDrainParams

    BENCHMARK_TARGETS = "canonical"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        if ctx.scenario != "enterprise_branch":
            return {"host_name": ctx.host0}
        return {
            "host_name": "br1_edge",
            "source_prefix": "10.1.40.0/24",
            "nat_ip_a": "198.18.1.10",
            "nat_ip_b": "198.18.1.11",
            "wan_interface": ctx.net_env.primary_wan_interface(),
        }

    def root_cause_resources(self, params: NatMappingRemovedWithoutDrainParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: NatMappingRemovedWithoutDrainParams):
        ops = native_fault_ops(self, params.host_name, "clear_nat_mappings")
        if ops is not None:
            wan, active = params.wan_interface, params.nat_ip_a
            self._before = {
                "addresses": ops.interface_addresses(wan),
                "mappings": ops.nat_mapping_count(active),
            }
            ops.add_source_nat(params.source_prefix, params.nat_ip_b, protocol="tcp")
            ops.remove_interface_address(wan, f"{active}/32")
            # Withdraw without draining: drop the mappings that used it.
            ops.clear_nat_mappings(active)
            return
        self._before = self.runtime.exec(
            params.host_name,
            f"ip -4 addr show dev {params.wan_interface}; "
            f"conntrack -L -p tcp --reply-dst {params.nat_ip_a} -o extended 2>/dev/null || true",
        )
        self.runtime.exec(
            params.host_name,
            "nft add chain ip nat snat_egress "
            "'{ type nat hook postrouting priority 98 ; policy accept; }' 2>/dev/null || true; "
            "nft flush chain ip nat snat_egress; "
            f"nft add rule ip nat snat_egress ip protocol tcp ip saddr {params.source_prefix} "
            f"snat to {params.nat_ip_b}; "
            f"ip addr del {params.nat_ip_a}/32 dev {params.wan_interface}",
        )
        self.runtime.exec(
            params.host_name,
            f"conntrack -D -p tcp --reply-dst {params.nat_ip_a} 2>/dev/null || true",
        )
        # Packets already in flight can otherwise create an UNREPLIED reverse
        # tuple after the address disappears.  It is not an active SNAT
        # mapping, but removing it keeps the tracking state consistent with an
        # immediate, non-draining address withdrawal.
        self.runtime.exec(
            params.host_name,
            f"conntrack -D -p tcp --orig-dst {params.nat_ip_a} 2>/dev/null || true",
        )

    def verify_fault(self, params: NatMappingRemovedWithoutDrainParams) -> dict:
        ops = native_fault_ops(self, params.host_name, "nat_mapping_count")
        if ops is not None:
            addresses = ops.interface_addresses(params.wan_interface)
            mappings = ops.nat_mapping_count(params.nat_ip_a)
            return build_verify_result(
                self.root_cause_name,
                f"{params.nat_ip_a}/32" not in addresses
                and f"{params.nat_ip_b}/32" in addresses
                and mappings == 0,
                {
                    "edge": params.host_name,
                    "before": getattr(self, "_before", ""),
                    "addresses": addresses,
                    "conntrack_mappings": mappings,
                },
            )
        addresses = self.runtime.exec(
            params.host_name, f"ip -4 addr show dev {params.wan_interface}"
        )
        entries = self.runtime.exec(
            params.host_name,
            f"conntrack -L -p tcp --reply-dst {params.nat_ip_a} -o extended 2>/dev/null || true",
        )
        return build_verify_result(
            self.root_cause_name,
            params.nat_ip_a not in addresses
            and params.nat_ip_b in addresses
            and not entries.strip(),
            {
                "edge": params.host_name,
                "before": getattr(self, "_before", ""),
                "addresses": addresses,
                "conntrack": entries,
            },
        )
