"""Forwarding and INT failures for the P4 gateway benchmark."""

from __future__ import annotations

import ipaddress

from pydantic import BaseModel, Field

from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import interface_resource, node_resource
from nika.problems.support.benchmark_targets import (
    p4_gateway_port_target,
    p4_port_options,
    resolve_path_mtu_target,
)
from nika.problems.support.compatible_columns import LINUX_PMTU_COLUMNS
from nika.problems.support.router_ops import native_fault_ops
from nika.problems.support.p4_gateway import (
    set_icmp_frag_needed_filter,
    set_int_mtu,
    ternary_miss_row,
)
from nika.runtime.base import LabRuntime
from nika.runtime.kathara.vde_proxy import KatharaVdeFaultProxy


def _read_switch(runtime: LabRuntime, switch: str) -> dict | None:
    """P4Runtime view of one switch from the controller, or ``None``."""
    from nika.net_env.p4_dc_fabric.fabric_manager.apply import run_manager

    try:
        payload = run_manager(runtime, "read", "--switch", switch, timeout=60)
        return payload["switches"][switch]
    except (RuntimeError, KeyError, TypeError):
        return None


def read_runtime_config_value(
    runtime: LabRuntime, switch: str, table: str, port: int
) -> int | None:
    """Read the live action parameter of ``table``'s entry for ``port`` via P4Runtime."""
    observed = _read_switch(runtime, switch)
    if observed is None:
        return None
    for row in (observed.get("runtime_config") or {}).get(table) or []:
        if row.get("is_default") or int(row.get("key", -1)) != int(port):
            continue
        values = [v for k, v in row.items() if k.startswith("param_")]
        if values:
            return int(values[0])
    return None


# nftables match for ICMP Destination Unreachable / Fragmentation Needed.
# Written the way ``nft list`` prints it (nft adds and hides the implied
# ``ip protocol icmp`` dependency), so verify_fault can match it exactly.
_FRAG_NEEDED_NFT_RULE = "icmp type destination-unreachable icmp code frag-needed drop"


def _age_out_path_mtus(runtime: LabRuntime, net_env) -> None:
    """Drop the path MTUs end hosts learned before the filter went in.

    Linux keeps a learned path MTU for ``net.ipv4.route.mtu_expires`` (10
    minutes); until then established destinations keep working. Re-installing
    each host's default route (same route, briefly another protocol tag)
    discards those entries now, which is the state the lab reaches once they
    expire.
    """
    from nika.runtime.spec import NodeRole
    from nika.utils.parallel import bounded_parallel_map

    hosts = [
        node
        for node, identity in (
            getattr(net_env, "machine_identities", None) or {}
        ).items()
        if getattr(identity, "role", None) in {NodeRole.HOST, NodeRole.SERVICE}
    ]
    command = (
        "r=$(ip -4 route show default | head -n1); "
        '[ -n "$r" ] && ip route replace $r proto static && ip route replace $r'
    )
    bounded_parallel_map(lambda host: runtime.exec(host, command), hosts)


class IcmpFragNeededFilterMisconfigurationParams(BaseModel):
    host_name: str = Field(
        default="gateway_1",
        description="Node that drops ICMP Fragmentation Needed (P4 gateway or Linux router).",
    )


class IcmpFragNeededFilterMisconfiguration(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name = "icmp_frag_needed_filter_misconfiguration"
    description = "ICMP Fragmentation Needed messages are filtered."
    symptom_desc = (
        "ICMP Fragmentation Needed is filtered, so PMTUD cannot shrink the path "
        "MTU and large transfers stall while small packets still work."
    )
    TAGS = ["icmp"]
    native_router_operations = frozenset(
        {"block_icmp_frag_needed", "icmp_frag_needed_blocked"}
    )
    COMPATIBLE_COLUMNS = LINUX_PMTU_COLUMNS | {"p4_dc_gateway"}
    Params = IcmpFragNeededFilterMisconfigurationParams
    BENCHMARK_TARGETS = "canonical"
    BENCHMARK_COORDINATES = frozenset({"mtu_mismatch"})

    @classmethod
    def benchmark_inject_params(cls, ctx):
        if ctx.scenario == "p4_dc_gateway":
            return {"host_name": "gateway_1"}
        # Linux / FRR path: drop Frag Needed on an intermediate router.
        mtu_target = resolve_path_mtu_target(
            ctx.scenario, ctx.net_env, ctx.rng, list(ctx.router_pool), ctx.backend
        )
        return {"host_name": mtu_target["host_name"]}

    @classmethod
    def coordinate_benchmark_inject(cls, ctx, params_by_problem):
        # PMTUD black hole: filter Frag Needed on the router that lowered the MTU.
        mtu = dict(params_by_problem["mtu_mismatch"])
        frag = dict(params_by_problem["icmp_frag_needed_filter_misconfiguration"])
        frag["host_name"] = mtu["host_name"]
        params_by_problem["icmp_frag_needed_filter_misconfiguration"] = frag
        return params_by_problem

    def root_cause_resources(self, params: IcmpFragNeededFilterMisconfigurationParams):
        return [node_resource(params.host_name)]

    def _uses_p4_filter(
        self, params: IcmpFragNeededFilterMisconfigurationParams
    ) -> bool:
        name = params.host_name
        if name.startswith("gateway_"):
            return True
        scenario = getattr(self, "scenario_name", None) or ""
        return scenario == "p4_dc_gateway"

    def inject_fault(self, params: IcmpFragNeededFilterMisconfigurationParams):
        if self._uses_p4_filter(params):
            self._result = set_icmp_frag_needed_filter(self.runtime, params.host_name)
            return
        ops = native_fault_ops(self, params.host_name, "block_icmp_frag_needed")
        if ops is not None:
            ops.block_icmp_frag_needed()
            self._result = f"{ops.profile}-filter-frag-needed"
            return
        self.runtime.add_nft_drop_rule(
            params.host_name, _FRAG_NEEDED_NFT_RULE, family="ip"
        )
        _age_out_path_mtus(self.runtime, self.net_env)
        self._result = "linux-nft-frag-needed"

    def verify_fault(self, params: IcmpFragNeededFilterMisconfigurationParams) -> dict:
        if self._uses_p4_filter(params):
            # Read the switch's live ACL: a (type 3, code 4) entry must be installed.
            observed = _read_switch(self.runtime, params.host_name) or {}
            rows = (observed.get("runtime_config") or {}).get("icmp_acl")
            entries = [
                row["keys"]
                for row in rows or []
                if not row.get("is_default") and row.get("keys")
            ]
            return build_verify_result(
                self.root_cause_name,
                [3, 4] in entries,
                {"gateway": params.host_name, "acl_entries": entries},
            )
        ops = native_fault_ops(self, params.host_name, "icmp_frag_needed_blocked")
        if ops is not None:
            return build_verify_result(
                self.root_cause_name,
                ops.icmp_frag_needed_blocked(),
                {"host": params.host_name, "profile": ops.profile},
            )
        verified = self.runtime.nft_drop_rule_present(
            params.host_name, _FRAG_NEEDED_NFT_RULE, family="ip"
        )
        return build_verify_result(
            self.root_cause_name,
            verified,
            {"host": params.host_name, "nft_rule": _FRAG_NEEDED_NFT_RULE},
        )


class P4TcamEntryCorruptionParams(BaseModel):
    host_name: str = Field(description="Gateway or spine whose route entry is corrupt.")
    target_ip: str = Field(
        description="Destination that the corrupted entry stops matching."
    )
    control_source: str = Field(description="Client used for the healthy control flow.")


def tcam_row(runtime: LabRuntime, params: P4TcamEntryCorruptionParams) -> dict:
    """The programmed route covering the target, its corrupt row, and its ports."""
    from nika.net_env.p4_dc_fabric.fabric_manager.apply import load_intent

    intent = load_intent(runtime)
    switch = intent["switches"][params.host_name]
    target = ipaddress.IPv4Address(params.target_ip)
    covering = [
        entry
        for entry in switch["ipv4_lpm"]
        if target in ipaddress.IPv4Network(entry["prefix"], strict=False)
    ]
    if not covering:
        raise RuntimeError(f"{params.host_name} has no route to {params.target_ip}")
    entry = max(
        covering,
        key=lambda e: ipaddress.IPv4Network(e["prefix"], strict=False).prefixlen,
    )
    others = [str(item["ip"]) for item in intent["endpoints"]]
    value, mask = ternary_miss_row(entry["prefix"], params.target_ip, others)
    group = next(g for g in switch["groups"] if g["group_id"] == entry["group_id"])
    ports = {m["member_id"]: m["port"] for m in switch["members"]}
    return {
        "prefix": entry["prefix"],
        "group_id": int(entry["group_id"]),
        "value": value,
        "mask": mask,
        "ports": sorted({int(ports[m]) for m in group["member_ids"]}),
    }


class P4TcamEntryCorruption(ProblemBase):
    """A soft error corrupts one programmed route entry in the switch's TCAM.

    One mask bit of the entry's host part flips from don't-care to care, so
    the hardware row no longer matches part of its prefix, including the
    target. Those destinations miss the route lookup on that switch, whichever
    next hop they would take, while the rest of the prefix and every other
    route keep forwarding. The corruption lives in the forwarding hardware,
    below the switch OS and P4Runtime: the controller's read still returns
    the programmed entry.

    The corrupt row is emulated outside every lab node: on each port of the
    route's next-hop group, a controller-side proxy drops the frames whose
    destination falls in the lost part of the prefix.
    """

    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name = "p4_tcam_entry_corruption"
    description = "A forwarding/TCAM entry is silently corrupted for one flow."
    symptom_desc = "One destination flow stops after a switch whose P4Runtime state remains healthy."
    TAGS = ["p4_runtime", "telemetry", "flow_tracking"]
    COMPATIBLE_COLUMNS = frozenset({"p4_dc_gateway"})
    supported_backends = ("kathara",)
    Params = P4TcamEntryCorruptionParams
    BENCHMARK_TARGETS = "canonical"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        if ctx.scenario != "p4_dc_gateway":
            return {"host_name": ctx.host0}
        rng = ctx.rng
        model = ctx.net_env.model
        service = rng.choice(model.services)
        target = rng.choice(model.gateways + model.spines)
        control = (
            next(
                client.name
                for client in model.clients
                if client.name != model.clients[0].name
            )
            if len(model.clients) > 1
            else model.clients[0].name
        )
        return {"host_name": target, "target_ip": service.ip, "control_source": control}

    def root_cause_resources(self, params: P4TcamEntryCorruptionParams):
        return [node_resource(params.host_name)]

    def _interfaces(self, params: P4TcamEntryCorruptionParams, row: dict) -> list[str]:
        ports = self.net_env.model.ports[params.host_name]
        return [port.name for port in ports if port.bmv2_port in row["ports"]]

    def inject_fault(self, params: P4TcamEntryCorruptionParams):
        row = tcam_row(self.runtime, params)
        controller = KatharaVdeFaultProxy(self.runtime)
        for intf in self._interfaces(params, row):
            state = controller.insert(params.host_name, intf)
            controller.set_dst_drop(state, row["value"], row["mask"])
        self._result = row

    def verify_fault(self, params: P4TcamEntryCorruptionParams) -> dict:
        """Each next-hop port drops the lost range; P4Runtime still reads the route."""
        row = tcam_row(self.runtime, params)
        controller = KatharaVdeFaultProxy(self.runtime)
        ports = {}
        for intf in self._interfaces(params, row):
            state = controller.discover(params.host_name, intf)
            ports[intf] = state is not None and controller.dst_drop_configured(
                state, row["value"], row["mask"]
            )
        corrupt = bool(ports) and all(ports.values())
        observed = _read_switch(self.runtime, params.host_name) or {}
        programmed = any(
            entry.get("prefix") == row["prefix"]
            and int(entry.get("group_id") or -1) == row["group_id"]
            for entry in observed.get("ipv4_lpm") or []
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=corrupt and programmed,
            details={
                "switch": params.host_name,
                "target_ip": params.target_ip,
                "route": row["prefix"],
                "corrupt_row": f"{row['value']}&&&{row['mask']}",
                "corrupt_on_ports": ports,
                "p4runtime_route_intact": programmed,
            },
        )

    def recover_fault(self, params: P4TcamEntryCorruptionParams) -> dict:
        row = tcam_row(self.runtime, params)
        controller = KatharaVdeFaultProxy(self.runtime)
        interfaces = self._interfaces(params, row)
        for intf in interfaces:
            state = controller.discover(params.host_name, intf)
            if state is not None:
                controller.remove(state)
        return {
            "verified": all(
                controller.discover(params.host_name, intf) is None
                for intf in interfaces
            ),
            "details": {"switch": params.host_name, "interfaces": interfaces},
        }


class IntInsufficientMtuHeadroomParams(BaseModel):
    host_name: str = Field(description="INT source gateway.")
    intf_name: str = Field(description="Gateway egress interface.")
    bmv2_port: int = Field(gt=0)
    int_mtu: int = Field(default=1480, ge=576, le=1500)


class IntInsufficientMtuHeadroom(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name = "int_insufficient_mtu_headroom"
    root_cause_owner = "interface"
    description = "INT encapsulation lacks sufficient MTU headroom."
    symptom_desc = "Near-MTU watched packets cannot carry the fixed INT-MX header."
    TAGS = ["p4_runtime", "int", "telemetry", "http"]
    COMPATIBLE_COLUMNS = frozenset({"p4_dc_gateway"})
    Params = IntInsufficientMtuHeadroomParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        if ctx.scenario != "p4_dc_gateway":
            return {"host_name": ctx.host0}
        ctx.rng.choice(ctx.net_env.model.services)  # keep the shared service draw
        params = p4_gateway_port_target(ctx, gateways_only=True)
        params["int_mtu"] = "1480"
        return params

    @classmethod
    def benchmark_inject_options(cls, ctx, base):
        return p4_port_options(ctx, base, gateways_only=True)

    def root_cause_resources(self, params: IntInsufficientMtuHeadroomParams):
        return [interface_resource(params.host_name, params.intf_name)]

    def inject_fault(self, params: IntInsufficientMtuHeadroomParams):
        set_int_mtu(self.runtime, params.host_name, params.bmv2_port, params.int_mtu)

    def verify_fault(self, params: IntInsufficientMtuHeadroomParams) -> dict:
        observed = read_runtime_config_value(
            self.runtime, params.host_name, "int_mtu_config", params.bmv2_port
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=observed == params.int_mtu,
            details={
                "interface": params.intf_name,
                "int_mtu": params.int_mtu,
                "observed_int_mtu": observed,
            },
        )
