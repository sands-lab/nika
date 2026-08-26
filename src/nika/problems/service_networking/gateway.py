"""Evidence-backed gateway and NAT resource failures."""

from __future__ import annotations

from pydantic import BaseModel, Field

from nika.problems.problem_base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.root_cause import node_resource
from nika.problems.support.p4_gateway import (
    exhaust_lb_conn_table,
    lb_state,
    unsafe_lb_pool_update,
)


class LbConnectionStateExhaustionParams(BaseModel):
    host_name: str = Field(default="gateway_1")
    capacity: int = Field(default=256, ge=32, le=256)
    syn_timeout_sec: int = Field(default=10, ge=1)
    seed: int = Field(default=1)


class LbConnectionStateExhaustion(ProblemBase):
    failure_domain = FailureDomain.SERVICE_NETWORKING
    root_cause_name = "lb_connection_state_exhaustion"
    symptom_desc = "SYN-only state exhausts the gateway ConnTable; pool churn then breaks evicted legitimate connections."
    TAGS = ["p4", "p4_runtime", "http", "l4_load_balancer"]
    Params = LbConnectionStateExhaustionParams

    def root_cause_resources(self, params: LbConnectionStateExhaustionParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: LbConnectionStateExhaustionParams):
        self._state = exhaust_lb_conn_table(
            self.runtime, params.host_name, params.capacity
        )
        # A pool update after the real table reaches capacity makes unmatched
        # packets select the replacement backend.
        self._update = unsafe_lb_pool_update(self.runtime, params.host_name)

    def verify_fault(self, params: LbConnectionStateExhaustionParams) -> dict:
        state = lb_state(self.runtime, params.host_name)
        tables = state.get("tables", {})
        return build_verify_result(
            self.root_cause_name,
            tables.get("lb_conn_table") == params.capacity
            and tables.get("lb_pool", 0) >= 128,
            {"gateway": params.host_name, "tables": tables},
        )


class LbPendingConnectionUpdateRaceParams(BaseModel):
    host_name: str = Field(default="gateway_1")
    learning_delay_ms: int = Field(default=5, ge=1, le=100)
    seed: int = Field(default=1)


class LbPendingConnectionUpdateRace(ProblemBase):
    failure_domain = FailureDomain.SERVICE_NETWORKING
    root_cause_name = "lb_pending_connection_update_race"
    symptom_desc = "An unsafe DIP pool update changes the selected backend before a pending connection reaches ConnTable."
    TAGS = ["p4", "p4_runtime", "http", "l4_load_balancer"]
    Params = LbPendingConnectionUpdateRaceParams

    def root_cause_resources(self, params: LbPendingConnectionUpdateRaceParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: LbPendingConnectionUpdateRaceParams):
        self._state = unsafe_lb_pool_update(self.runtime, params.host_name)

    def verify_fault(self, params: LbPendingConnectionUpdateRaceParams) -> dict:
        state = lb_state(self.runtime, params.host_name)
        tables = state.get("tables", {})
        return build_verify_result(
            self.root_cause_name,
            tables.get("lb_vip") == 1 and tables.get("lb_pool", 0) >= 128,
            {"gateway": params.host_name, "tables": tables},
        )


class SnatPortPoolExhaustionParams(BaseModel):
    host_name: str = Field(default="br1_edge")
    source_prefix: str = Field(default="10.1.40.0/24")
    public_ip: str
    port_start: int = Field(default=40000, ge=1024, le=65535)
    port_end: int = Field(default=40063, ge=1024, le=65535)


class SnatPortPoolExhaustion(ProblemBase):
    failure_domain = FailureDomain.SERVICE_NETWORKING
    root_cause_name = "snat_port_pool_exhaustion"
    symptom_desc = "The available SNAT source-port pool is exhausted for concurrent outbound connections."
    TAGS = ["vpn", "http", "nat"]
    Params = SnatPortPoolExhaustionParams

    def root_cause_resources(self, params: SnatPortPoolExhaustionParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: SnatPortPoolExhaustionParams):
        if params.port_end < params.port_start:
            raise ValueError("port_end must be greater than or equal to port_start")
        self.runtime.exec(
            params.host_name,
            "nft add chain ip nat nika_snat_pool "
            "'{ type nat hook postrouting priority 99 ; policy accept; }' 2>/dev/null || true",
        )
        self.runtime.exec(params.host_name, "nft flush chain ip nat nika_snat_pool")
        self.runtime.exec(
            params.host_name,
            f"nft add rule ip nat nika_snat_pool ip protocol tcp "
            f"ip saddr {params.source_prefix} "
            f"snat to {params.public_ip}:{params.port_start}-{params.port_end} "
            "comment 'nika-snat-pool'",
        )

    def verify_fault(self, params: SnatPortPoolExhaustionParams) -> dict:
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
    root_cause_name = "nat_mapping_removed_without_drain"
    symptom_desc = (
        "An active SNAT address is removed without draining its conntrack mappings."
    )
    TAGS = ["vpn", "http", "nat"]
    Params = NatMappingRemovedWithoutDrainParams

    def root_cause_resources(self, params: NatMappingRemovedWithoutDrainParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: NatMappingRemovedWithoutDrainParams):
        self._before = self.runtime.exec(
            params.host_name,
            f"ip -4 addr show dev {params.wan_interface}; "
            f"conntrack -L -p tcp --reply-dst {params.nat_ip_a} -o extended 2>/dev/null || true",
        )
        self.runtime.exec(
            params.host_name,
            "nft add chain ip nat nika_nat_failover "
            "'{ type nat hook postrouting priority 98 ; policy accept; }' 2>/dev/null || true; "
            "nft flush chain ip nat nika_nat_failover; "
            f"nft add rule ip nat nika_nat_failover ip protocol tcp ip saddr {params.source_prefix} "
            f"snat to {params.nat_ip_b} comment 'nika-nat-failover'; "
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
