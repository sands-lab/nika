"""Forwarding and INT failures for the P4 gateway benchmark."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from nika.problems.support.nft_recovery import NftDropRecovery
from nika.problems.support.p4runtime_writes import p4runtime_write, require_ok
from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource
from nika.problems.support.benchmark_targets import (
    resolve_path_mtu_target,
)
from nika.problems.support.compatible_columns import (
    LINUX_PMTU_COLUMNS,
    LINUX_PMTU_PLATFORMS,
)
from nika.problems.support.p4_oracle import (
    table_read,
)
from nika.problems.support.oracle import (
    FILTER_DUMP,
    any_of,
    iptables_rule,
    OraclePlaybook,
    OracleStep,
    contains,
    shell,
    iosxr,
)
from nika.problems.support.router_ops import native_fault_ops, native_router_profile
from nika.problems.support.p4_gateway import (
    set_icmp_frag_needed_filter,
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
    CONFUSABLE_WITH = ("mtu_mismatch",)
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

    @classmethod
    def benchmark_inject_params(cls, ctx):
        if ctx.scenario == "p4_dc_gateway":
            return {"host_name": "gateway_1"}
        # Linux / FRR path: drop Frag Needed on an intermediate router.
        mtu_target = resolve_path_mtu_target(
            ctx.scenario, ctx.net_env, ctx.rng, list(ctx.router_pool), ctx.backend
        )
        return {"host_name": mtu_target["host_name"]}

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

    @classmethod
    def is_compatible(cls, target, device_profile=None, *, backend=None):
        if device_profile == "sonic":
            return False
        return super().is_compatible(target, device_profile, backend=backend)

    @classmethod
    def runs_on_device_profile(cls, device_profile):
        return device_profile != "sonic" and super().runs_on_device_profile(
            device_profile
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
                    "command": "/ip firewall filter print terse",
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
                "nft list ruleset"
                if self.net_env.machine_identities[host].role.value
                in {"router", "switch"}
                else FILTER_DUMP,
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
