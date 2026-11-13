"""Forwarding and INT failures for the P4 gateway benchmark."""

from __future__ import annotations

import ipaddress
import re

from pydantic import BaseModel, Field

from nika.problems.support.nft_recovery import NftDropRecovery
from nika.problems.support.p4runtime_writes import p4runtime_write, require_ok
from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import interface_resource, node_resource
from nika.problems.support.benchmark_targets import (
    NoValidTargetError,
    p4_gateway_port_target,
    p4_port_options,
    resolve_path_mtu_target,
)
from nika.problems.support.compatible_columns import (
    LINUX_PMTU_COLUMNS,
    LINUX_PMTU_PLATFORMS,
)
from nika.problems.support.p4_oracle import (
    table_read,
    bmv2_ports,
)
from nika.problems.support.oracle import (
    FILTER_DUMP,
    any_of,
    iptables_rule,
    Check,
    OraclePlaybook,
    OracleStep,
    contains,
    shell,
    iosxr,
)
from nika.problems.support.router_ops import native_fault_ops, native_router_profile
from nika.problems.support.p4_gateway import (
    set_icmp_frag_needed_filter,
    set_int_mtu,
    ternary_miss_row,
    read_table,
)
from nika.runtime.base import LabRuntime


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
        {
            "block_icmp_frag_needed",
            "icmp_frag_needed_blocked",
            "save_config",
            "restore_config",
        }
    )
    COMPATIBLE_COLUMNS = LINUX_PMTU_COLUMNS | {"p4_dc_gateway"}
    COMPATIBLE_PLATFORMS = LINUX_PMTU_PLATFORMS
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
        if native_router_profile(ctx.net_env, mtu["host_name"]) == "iosxr":
            eligible = [
                node
                for node in ctx.net_env.routers or []
                if native_router_profile(ctx.net_env, node) != "iosxr"
            ]
            if not eligible:
                raise NoValidTargetError(
                    "The PMTU combination needs a router whose filter covers "
                    "locally generated ICMP."
                )
            mtu.update(
                resolve_path_mtu_target(
                    ctx.scenario, ctx.net_env, ctx.rng, eligible, ctx.backend
                )
            )
            params_by_problem["mtu_mismatch"] = mtu
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
            observed = _read_switch(self.runtime, params.host_name)
            if observed is None:
                raise RuntimeError("Cannot read the ICMP filter before injection")
            self._original_icmp_acl = list(
                (observed.get("runtime_config") or {}).get("icmp_acl") or []
            )
            self._result = set_icmp_frag_needed_filter(self.runtime, params.host_name)
            return
        ops = native_fault_ops(self, params.host_name, "block_icmp_frag_needed")
        if ops is not None:
            self._native_saved = (ops, ops.save_config())
            self._native_blocked = ops.icmp_frag_needed_blocked()
            ops.block_icmp_frag_needed()
            if ops.profile == "iosxr":
                _age_out_path_mtus(self.runtime, self.net_env)
            self._result = f"{ops.profile}-filter-frag-needed"
            return
        self._nft_recovery = NftDropRecovery()
        self._nft_recovery.add(
            self.runtime, params.host_name, _FRAG_NEEDED_NFT_RULE, family="ip"
        )
        _age_out_path_mtus(self.runtime, self.net_env)
        self._result = "linux-nft-frag-needed"

    def recover_fault(self, params: IcmpFragNeededFilterMisconfigurationParams) -> dict:
        if self._uses_p4_filter(params):
            original = getattr(self, "_original_icmp_acl", None)
            if original is None:
                ok = False
            else:
                observed = _read_switch(self.runtime, params.host_name)
                if observed is None:
                    raise RuntimeError("Cannot read the ICMP filter during recovery")
                present = any(
                    r.get("keys") == [3, 4] and not r.get("is_default")
                    for r in (observed.get("runtime_config") or {}).get("icmp_acl", [])
                )
                was_present = any(
                    r.get("keys") == [3, 4] and not r.get("is_default")
                    for r in original
                )
                if present and not was_present:
                    require_ok(
                        p4runtime_write(
                            self.runtime,
                            params.host_name,
                            [
                                {
                                    "type": "DELETE",
                                    "table": "icmp_acl",
                                    "match": {"type": 3, "code": 4},
                                }
                            ],
                        ),
                        "ICMP filter removal",
                    )
                current = _read_switch(self.runtime, params.host_name)
                ok = (
                    current is not None
                    and ((current.get("runtime_config") or {}).get("icmp_acl") or [])
                    == original
                )
        elif hasattr(self, "_native_saved"):
            ops, saved = self._native_saved
            ops.restore_config(saved)
            ok = ops.icmp_frag_needed_blocked() == self._native_blocked
        else:
            recovery = getattr(self, "_nft_recovery", None)
            ok = recovery is not None and recovery.restore(self.runtime)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=ok,
            details={"host": params.host_name},
        )

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

    def oracle_playbook(
        self, params: IcmpFragNeededFilterMisconfigurationParams
    ) -> OraclePlaybook:
        host = params.host_name
        finding = (
            f"{host} drops ICMP Fragmentation Needed (type 3 code 4), so senders "
            "never learn the smaller path MTU and large DF packets are black-holed."
        )
        if self._uses_p4_filter(params):
            model = self.net_env.model
            client = model.client_on_gateway(host)
            service = model.services[0]
            return OraclePlaybook(
                finding=finding,
                steps=[
                    shell(
                        service.name,
                        f"ip route get {client.ip}; ping -M do -s 1450 -c 3 -W 1 "
                        f"{client.ip} 2>&1 | grep -E 'mtu|Frag|transmitted'",
                        f"Send full-size DF packets from {service.name} to "
                        f"{client.name}, behind {host}'s smaller access MTU; a "
                        "cached path MTU predates the filter.",
                    ),
                    table_read(
                        host,
                        "icmp_acl",
                        "an icmp_acl entry drops ICMP type 3 code 4",
                        lambda rows: any(
                            row["match"] == {"type": 3, "code": 4}
                            and row["action"] == "drop"
                            for row in rows
                        ),
                        f"List the ICMP ACL entries on {host}.",
                    ),
                ],
            )
        if native_router_profile(self.net_env, host) == "iosxr":
            from nika.net_env.utils.iosxr.fault_ops import _FRAG_FILTER_ACL

            return OraclePlaybook(
                finding=finding,
                steps=[
                    iosxr(
                        host,
                        f"show running-config ipv4 access-list {_FRAG_FILTER_ACL}",
                        f"Read the IPv4 ACLs on {host}.",
                        contains(
                            r"deny icmp any any packet-too-big",
                            "an ACL denies ICMP type 3 code 4",
                        ),
                        evidence=True,
                    ),
                    iosxr(
                        host,
                        "show running-config interface",
                        f"Read the interface ACL bindings on {host}.",
                        contains(
                            rf"ipv4 access-group {re.escape(_FRAG_FILTER_ACL)} egress",
                            "the ICMP ACL is bound to egress",
                        ),
                        evidence=True,
                    ),
                ],
            )
        if native_router_profile(self.net_env, host) == "routeros":
            step = OracleStep(
                f"List the ICMP firewall filters on {host}.",
                "routeros_exec",
                {
                    "router_name": host,
                    "command": "/ip firewall filter print terse where protocol=icmp",
                },
                contains(
                    r"icmp-options=3:4.*action=drop|action=drop.*icmp-options=3:4",
                    "a filter drops ICMP type 3 code 4",
                ),
                evidence=True,
            )
        else:
            step = shell(
                host,
                FILTER_DUMP,
                f"Inspect the packet filter on {host} for ICMP rules.",
                any_of(
                    contains(r"frag-needed.*drop", "a filter drops ICMP frag-needed"),
                    iptables_rule(
                        r"--icmp-type (3/4|fragmentation-needed)",
                        "a filter drops ICMP frag-needed",
                    ),
                ),
                evidence=True,
            )
        return OraclePlaybook(finding=finding, steps=[step])


class P4TcamEntryCorruptionParams(BaseModel):
    host_name: str = Field(description="Gateway or spine whose route entry is corrupt.")
    target_ip: str = Field(
        description="Destination that the corrupted entry stops matching."
    )
    control_source: str = Field(description="Client used for the healthy control flow.")


def _tcam_row(intent: dict, params: P4TcamEntryCorruptionParams) -> dict:
    """The intended prefix, selector group, and changed device mask for the target."""
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
    _, mask = ternary_miss_row(entry["prefix"], params.target_ip, others)
    return {
        "prefix": entry["prefix"],
        "group_id": int(entry["group_id"]),
        "mask": mask,
    }


def tcam_row(runtime: LabRuntime, params: P4TcamEntryCorruptionParams) -> dict:
    from nika.net_env.p4_dc_fabric.fabric_manager.apply import load_intent

    return _tcam_row(load_intent(runtime), params)


class P4TcamEntryCorruption(ProblemBase):
    """A route's ternary mask gains one care bit while controller intent stays intact.

    The injector writes the switch table directly through a separate P4Runtime
    client. The changed match is visible in device reads and absent from intent.
    """

    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name = "p4_tcam_entry_corruption"
    description = "A ternary forwarding route has an incorrect match mask."
    symptom_desc = "One destination loses its route on a switch whose table differs from controller intent."
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

    def _write_row(self, params: P4TcamEntryCorruptionParams, *, corrupt: bool) -> None:
        row = tcam_row(self.runtime, params)
        network = ipaddress.IPv4Network(row["prefix"])
        original = {"prefix": row["prefix"], "group_id": row["group_id"]}
        changed = {
            **original,
            "value": str(network.network_address),
            "mask": row["mask"],
        }
        before, after = (original, changed) if corrupt else (changed, original)
        # A match key cannot be modified in place. Recovery also handles a
        # failed insertion after the original row was deleted.
        entries = read_table(self.runtime, params.host_name, "ipv4_lpm")
        if entries is None:
            raise RuntimeError("Cannot read the route table before updating its mask")
        expected = [
            int(network.network_address),
            int(ipaddress.IPv4Address(before.get("mask", str(network.netmask)))),
        ]
        updates = []
        if any(entry.get("match", {}).get("dstAddr") == expected for entry in entries):
            updates.append({"type": "DELETE", "lpm": before})
        wanted = [
            int(network.network_address),
            int(ipaddress.IPv4Address(after.get("mask", str(network.netmask)))),
        ]
        if not any(
            entry.get("match", {}).get("dstAddr") == wanted for entry in entries
        ):
            updates.append({"lpm": after})
        require_ok(
            p4runtime_write(self.runtime, params.host_name, updates, sequential=True),
            "route mask update",
        )

    def inject_fault(self, params: P4TcamEntryCorruptionParams):
        self._write_row(params, corrupt=True)

    def _verify_row(
        self, params: P4TcamEntryCorruptionParams, *, corrupt: bool
    ) -> dict:
        row = tcam_row(self.runtime, params)
        network = ipaddress.IPv4Network(row["prefix"])
        normal = [int(network.network_address), int(network.netmask)]
        changed = [normal[0], int(ipaddress.IPv4Address(row["mask"]))]
        wanted, absent = (changed, normal) if corrupt else (normal, changed)
        entries = read_table(self.runtime, params.host_name, "ipv4_lpm")
        verified = (
            entries is not None
            and any(
                entry.get("match", {}).get("dstAddr") == wanted
                and entry.get("priority") == network.prefixlen + 1
                and entry.get("group_id") == row["group_id"]
                for entry in entries
            )
            and not any(
                entry.get("match", {}).get("dstAddr") == absent for entry in entries
            )
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "switch": params.host_name,
                "expected_match": wanted,
                "group_id": row["group_id"],
            },
        )

    def verify_fault(self, params: P4TcamEntryCorruptionParams) -> dict:
        return self._verify_row(params, corrupt=True)

    def recover_fault(self, params: P4TcamEntryCorruptionParams) -> dict:
        self._write_row(params, corrupt=False)
        return self._verify_row(params, corrupt=False)

    def oracle_playbook(self, params: P4TcamEntryCorruptionParams) -> OraclePlaybook:
        switch, target = params.host_name, params.target_ip
        model = self.net_env.model
        on_gateway = switch in model.gateways
        sources = (
            [model.client_on_gateway(switch).name]
            if on_gateway
            else [client.name for client in model.clients]
        )
        traffic = [
            shell(
                src,
                f"ping -c 5 -i 0.2 -W 1 {target} 2>&1 | tail -2; "
                "for i in $(seq 8); do curl -s -o /dev/null -m 1 "
                f"-w '%{{http_code}} ' http://{target}/; done",
                f"Send ICMP and HTTP probes from {src} to {target}.",
                timeout=40,
            )
            for src in sources
        ]
        if on_gateway and params.control_source not in sources:
            traffic.append(
                shell(
                    params.control_source,
                    f"ping -c 3 -W 1 {target} 2>&1 | tail -2",
                    f"Control: reach {target} from {params.control_source}, "
                    f"which does not cross {switch}.",
                    contains(r"\b[1-9][0-9]* received", "probes get replies"),
                )
            )

        from nika.problems.forwarding_encapsulation_policy.p4_runtime import (
            declared_intent,
        )

        row = _tcam_row(declared_intent(self.net_env), params)
        expected = [
            int(ipaddress.IPv4Network(row["prefix"]).network_address),
            int(ipaddress.IPv4Address(row["mask"])),
        ]

        return OraclePlaybook(
            finding=(
                f"{switch}'s ternary route stops matching part of the prefix "
                f"that covers {target}: packets to {target} miss the lookup on {switch}, "
                "while the device route differs from controller intent and other "
                "destinations on that route forward."
            ),
            steps=[
                *traffic,
                table_read(
                    switch,
                    "ipv4_lpm",
                    "The route mask contains an extra care bit",
                    lambda entries: (
                        any(
                            entry.get("match", {}).get("dstAddr") == expected
                            and entry.get("priority")
                            == ipaddress.IPv4Network(row["prefix"]).prefixlen + 1
                            and entry.get("group_id") == row["group_id"]
                            for entry in entries
                        )
                        and not any(
                            entry.get("match", {}).get("dstAddr")
                            == [
                                expected[0],
                                int(ipaddress.IPv4Network(row["prefix"]).netmask),
                            ]
                            for entry in entries
                        )
                    ),
                    f"Read the live ternary route masks on {switch} and compare with its intended prefix.",
                ),
            ],
        )


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
        self._original_int_mtu = read_runtime_config_value(
            self.runtime, params.host_name, "int_mtu_config", params.bmv2_port
        )
        if self._original_int_mtu is None:
            raise RuntimeError("Cannot read the INT MTU before injection")
        set_int_mtu(self.runtime, params.host_name, params.bmv2_port, params.int_mtu)

    def recover_fault(self, params: IntInsufficientMtuHeadroomParams) -> dict:
        original = getattr(self, "_original_int_mtu", None)
        if original is not None:
            set_int_mtu(self.runtime, params.host_name, params.bmv2_port, original)
        observed = read_runtime_config_value(
            self.runtime, params.host_name, "int_mtu_config", params.bmv2_port
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=original is not None and observed == original,
            details={"original_mtu": original, "observed_mtu": observed},
        )

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

    def oracle_playbook(
        self, params: IntInsufficientMtuHeadroomParams
    ) -> OraclePlaybook:
        switch, intf, port = params.host_name, params.intf_name, params.bmv2_port

        def lowered(rows: list[dict]) -> bool:
            mtus = {
                int(row["match"]["egress_spec"]): int(row["params"]["mtu"])
                for row in rows
            }
            return mtus.get(port) == params.int_mtu and any(
                mtu > params.int_mtu for p, mtu in mtus.items() if p != port
            )

        def link_mtu_above(output: str) -> bool:
            m = re.search(r"\bmtu (\d+)", output)
            return bool(m) and int(m.group(1)) > params.int_mtu

        return OraclePlaybook(
            finding=(
                f"The INT MTU of {switch}:{intf} (BMv2 port {port}) is "
                f"{params.int_mtu}, below the link MTU, so near-MTU watched packets "
                "leave without the INT-MX header and drop out of telemetry."
            ),
            steps=[
                bmv2_ports(
                    switch,
                    contains(rf"(^|\s){port}@{intf}\b", f"port {port} is {intf}"),
                ),
                table_read(
                    switch,
                    "int_mtu_config",
                    f"port {port} has INT MTU {params.int_mtu} while other ports "
                    "allow more",
                    lowered,
                    f"Read the per-port INT MTU on {switch}.",
                ),
                shell(
                    switch,
                    f"ip link show dev {intf} | head -1",
                    f"Read the link MTU of {switch}:{intf}.",
                    Check(f"the link MTU exceeds {params.int_mtu}", link_mtu_above),
                    evidence=True,
                ),
            ],
        )
