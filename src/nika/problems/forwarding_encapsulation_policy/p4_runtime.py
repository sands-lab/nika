"""P4Runtime / ActionSelector failures for shared P4 forwarding intent."""

from __future__ import annotations

import copy
import ipaddress
import json
import random
import re
import shlex
from pathlib import Path

from pydantic import BaseModel, Field

from nika.problems.support.traffic import LOSS_EVIDENCE_TRAFFIC
from nika.problems.base import (
    FailureDomain,
    ProblemBase,
    build_verify_result,
)
from nika.problems.rca import node_resource
from nika.problems.support.benchmark_targets import (
    bmv2_leaves,
    choice,
    prefer_prefixed_node,
)
from nika.problems.forwarding_encapsulation_policy.p4runtime_helpers import (
    ecmp_target as _ecmp_target,
    gateway_backend_leaf,
    load_blackhole_pipeline,
    remove_staged_pipeline,
    load_intent,
    lpm_capacity,
    run_manager,
)
from nika.problems.support.p4_oracle import bmv2_ports, summary_regex
from nika.problems.support.oracle import (
    p4rt,
    Check,
    OraclePlaybook,
    OracleStep,
    all_of,
    contains,
    lacks,
    shell,
)
from nika.problems.support.p4runtime_writes import p4runtime_write, require_ok
from nika.utils.logger import system_logger

logger = system_logger

_BAD_PORT = 31
_BLACKHOLE_P4 = Path(__file__).with_name("blackhole.p4")
CONTROLLER = "fabric_mgr"
CONTROLLER_LOG = "/var/log/p4rt/controller.log"


# ---------------------------------------------------------------------------
# Oracle helpers shared by the P4 failures
# ---------------------------------------------------------------------------


def declared_intent(net_env) -> dict:
    """The forwarding intent the controller installs, rebuilt from the topology.

    Playbooks target their steps from it so they build without a running lab;
    the controller writes the same deterministic intent at startup.
    """
    from nika.net_env.p4_dc_fabric.fabric_manager.intent import (
        build_forwarding_intent,
    )
    from nika.net_env.p4_dc_gateway.control import build_gateway_intent
    from nika.net_env.p4_dc_gateway.topology_model import GatewayFabricModel

    model = net_env.model
    if isinstance(model, GatewayFabricModel):
        return build_gateway_intent(model)
    return build_forwarding_intent(model)


# The checks below match single JSON objects in the text, so they hold on both
# ``p4rt_exec`` output and the controller's compact view from ``switch_state``.


def _member_port(output: str, member_id: int) -> int | None:
    m = re.search(rf'"member_id":\s*{member_id},[^{{}}]*?"port":\s*(\d+)', output)
    return int(m.group(1)) if m else None


def _group_members(output: str, group_id: int) -> list[int] | None:
    m = re.search(rf'"group_id":\s*{group_id},\s*"member_ids":\s*\[([^\]]*)\]', output)
    return [int(x) for x in re.findall(r"\d+", m.group(1))] if m else None


def _route_group(output: str, prefix: str) -> int | None:
    m = re.search(rf'"prefix":\s*"{re.escape(prefix)}",\s*"group_id":\s*(\d+)', output)
    return int(m.group(1)) if m else None


def member_port_is(member_id: int, port: int) -> Check:
    return Check(
        f"member {member_id} forwards to port {port}",
        lambda o: _member_port(o, member_id) == port,
    )


def member_defined(member_id: int) -> Check:
    return Check(
        f"member {member_id} is programmed",
        lambda o: _member_port(o, member_id) is not None,
    )


def group_has(group_id: int, member_id: int, present: bool = True) -> Check:
    def check(output: str) -> bool:
        members = _group_members(output, group_id)
        return bool(members) and (member_id in members) == present

    verb = "contains" if present else "still has members but not"
    return Check(f"group {group_id} {verb} member {member_id}", check)


def route_group_is(prefix: str, group_id: int) -> Check:
    return Check(
        f"route {prefix} uses group {group_id}",
        lambda o: _route_group(o, prefix) == group_id,
    )


def mismatch(switch: str, kind: str) -> Check:
    return contains(
        summary_regex(switch, rf'"mismatches":\s*\[[^\]]*"{kind}"'),
        f"the read flags an intent mismatch in {kind}",
    )


# ``p4rt_exec read`` prints every member, counter, and table row, so on large
# switches the head/tail-truncated tool output hides the groups and routes. The
# controller's own CLI piped through a JSON filter shows just the forwarding
# state; the lock serializes it with ``p4rt_exec`` like every manager client.
_FORWARDING_VIEW = (
    'import json, sys; s = sys.argv[1]; v = json.load(sys.stdin)["switches"][s]; '
    'print(json.dumps({s: {"mismatches": v["mismatches"]}, '
    '"members": [{"member_id": m["member_id"], "port": m["port"]} '
    'for m in v["members"]], "groups": v["groups"], "ipv4_lpm": v["ipv4_lpm"]}))'
)


def switch_state(switch: str, purpose: str, *checks: Check) -> OracleStep:
    """Evidence from the live members, groups, and routes of one switch."""
    from nika.net_env.p4_dc_fabric.fabric_manager.apply import MANAGER
    from nika.service.kathara.bmv2_api import LOCK_FILE

    return shell(
        CONTROLLER,
        f"flock -w 80 {LOCK_FILE} python3 {MANAGER} read --switch {switch} "
        f"| python3 -c {shlex.quote(_FORWARDING_VIEW)} {switch}",
        purpose,
        all_of(*checks),
        evidence=True,
        timeout=90,
    )


def bmv2_running(switch: str) -> OracleStep:
    return shell(
        switch,
        "pgrep -af '[s]imple_switch' | grep -v -e '<defunct>' -e '^[0-9]* \\[' "
        "|| echo NO_PROCESS",
        f"Check that the BMv2 dataplane on {switch} is running.",
        all_of(
            contains(r"simple_switch", "a simple_switch process is running"),
            lacks(r"^NO_PROCESS$"),
        ),
    )


def intent_drift(switch: str, kind: str) -> OracleStep:
    """The ``read`` summary flags *switch* as diverging from the intent in *kind*."""
    return p4rt(
        "read",
        "Compare every switch's live P4Runtime state with the controller intent.",
        mismatch(switch, kind),
    )


def scope_ping(intent: dict, switch: str, dst: dict | None = None) -> list[OracleStep]:
    """Probe across *switch* from one of its attached hosts, when it has one."""
    local = attached_endpoints(intent, switch)
    if not local:
        return []
    src = local[0]
    remote = [e for e in intent.get("endpoints") or [] if e not in local]
    dst = dst or next(
        (e for e in remote if e.get("role") != src.get("role")),
        remote[0] if remote else None,
    )
    if dst is None:
        return []
    return [
        shell(
            src["name"],
            f"ping -c 3 -W 1 {dst['ip']} 2>&1 | tail -3",
            f"Probe {dst['name']} ({dst['ip']}) from {src['name']} through {switch}.",
            contains(r" 0 received|100% packet loss", "every probe is lost"),
        )
    ]


def attached_endpoints(intent: dict, switch: str) -> list[dict]:
    """End hosts that are direct next hops of *switch*."""
    peers = {str(m.get("peer")) for m in intent["switches"][switch]["members"]}
    return [e for e in intent.get("endpoints") or [] if e["name"] in peers]


def endpoint_in(intent: dict, prefix: str, exclude: list[str]) -> dict | None:
    """An end host covered by *prefix*, other than the names in *exclude*."""
    net = ipaddress.ip_network(prefix, strict=False)
    return next(
        (
            e
            for e in intent.get("endpoints") or []
            if ipaddress.ip_address(e["ip"]) in net and e["name"] not in exclude
        ),
        None,
    )


class P4ActionSelectorMemberMisconfigParams(BaseModel):
    host_name: str = Field(description="Target BMv2 switch name.")


class P4ActionSelectorMemberMisconfig(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    symptom_probe = "custom"
    symptom_class = "loss"
    root_cause_name = "p4_action_selector_member_misconfig"
    TRAFFIC_PROFILE = LOSS_EVIDENCE_TRAFFIC
    description = "A P4 ActionSelector member is misconfigured."
    TAGS = ["p4", "p4_runtime"]
    Params = P4ActionSelectorMemberMisconfigParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"host_name": choice(ctx.rng, bmv2_leaves(ctx.bmv2), "leaf_1")}

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._target: dict | None = None

    def root_cause_resources(self, params: P4ActionSelectorMemberMisconfigParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: P4ActionSelectorMemberMisconfigParams):
        intent = load_intent(self.runtime)
        prefix, group_id, member_id, peer = _ecmp_target(intent, params.host_name)
        member = next(
            dict(m)
            for m in intent["switches"][params.host_name]["members"]
            if int(m["member_id"]) == int(member_id)
        )
        member["port"] = _BAD_PORT
        result = require_ok(
            p4runtime_write(
                self.runtime,
                params.host_name,
                [{"type": "MODIFY", "member": member}],
            ),
            f"member update on {params.host_name}",
        )
        self._target = {
            "prefix": prefix,
            "group_id": group_id,
            "member_id": member_id,
            "peer": peer,
            "port": _BAD_PORT,
            "result": result,
        }
        logger.info(
            "Misconfigured ActionSelector member %s on %s (port %s)",
            member_id,
            params.host_name,
            _BAD_PORT,
        )

    def verify_fault(self, params: P4ActionSelectorMemberMisconfigParams) -> dict:
        intent = load_intent(self.runtime)
        prefix, group_id, member_id, peer = _ecmp_target(intent, params.host_name)
        target = self._target or {
            "prefix": prefix,
            "group_id": group_id,
            "member_id": member_id,
            "peer": peer,
            "port": _BAD_PORT,
        }
        observed = run_manager(
            self.runtime, "read", "--switch", params.host_name, timeout=60
        )
        members = (observed.get("switches") or {}).get(params.host_name, {}).get(
            "members"
        ) or []
        got = next(
            (
                m
                for m in members
                if int(m.get("member_id") or 0) == int(target.get("member_id") or -1)
            ),
            None,
        )
        member_ok = got is not None and int(got.get("port") or 0) == _BAD_PORT
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=member_ok,
            details={
                "host": params.host_name,
                "member": got,
                "target": target,
            },
        )

    def oracle_playbook(
        self, params: P4ActionSelectorMemberMisconfigParams
    ) -> OraclePlaybook:
        switch = params.host_name
        prefix, group_id, member_id, peer = _ecmp_target(
            declared_intent(self.net_env), switch
        )

        return OraclePlaybook(
            finding=(
                f"ActionSelector member {member_id} of ECMP group {group_id} "
                f"({prefix}) on {switch} forwards to port {_BAD_PORT}, which has no "
                f"interface, instead of toward {peer}, so flows hashed onto it are "
                "dropped."
            ),
            steps=[
                intent_drift(switch, "members"),
                switch_state(
                    switch,
                    f"Read the ActionSelector members and groups on {switch}.",
                    member_port_is(member_id, _BAD_PORT),
                    group_has(group_id, member_id),
                    mismatch(switch, "members"),
                ),
                bmv2_ports(
                    switch,
                    lacks(
                        rf"(^|\s){_BAD_PORT}@",
                        f"no interface is bound to BMv2 port {_BAD_PORT}",
                    ),
                ),
            ],
        )


class P4EcmpGroupMemberMissingParams(BaseModel):
    host_name: str = Field(description="Target BMv2 switch name.")


class P4EcmpGroupMemberMissing(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    symptom_probe = "custom"
    symptom_class = "degradation"
    root_cause_name = "p4_ecmp_group_member_missing"
    TRAFFIC_PROFILE = LOSS_EVIDENCE_TRAFFIC
    description = "A member is missing from a P4 ECMP group."
    TAGS = ["p4", "p4_runtime"]
    Params = P4EcmpGroupMemberMissingParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"host_name": choice(ctx.rng, bmv2_leaves(ctx.bmv2), "leaf_1")}

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._target: dict | None = None

    def root_cause_resources(self, params: P4EcmpGroupMemberMissingParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: P4EcmpGroupMemberMissingParams):
        intent = load_intent(self.runtime)
        prefix, group_id, member_id, peer = _ecmp_target(intent, params.host_name)
        group = next(
            g
            for g in intent["switches"][params.host_name]["groups"]
            if int(g["group_id"]) == int(group_id)
        )
        remaining = [m for m in group["member_ids"] if int(m) != int(member_id)]
        result = require_ok(
            p4runtime_write(
                self.runtime,
                params.host_name,
                [
                    {
                        "type": "MODIFY",
                        "group": {"group_id": group_id, "member_ids": remaining},
                    }
                ],
            ),
            f"group update on {params.host_name}",
        )
        self._target = {
            "prefix": prefix,
            "group_id": group_id,
            "member_id": member_id,
            "peer": peer,
            "result": result,
        }

    def verify_fault(self, params: P4EcmpGroupMemberMissingParams) -> dict:
        intent = load_intent(self.runtime)
        prefix, group_id, member_id, peer = _ecmp_target(intent, params.host_name)
        target = self._target or {
            "prefix": prefix,
            "group_id": group_id,
            "member_id": member_id,
            "peer": peer,
        }
        observed = run_manager(
            self.runtime, "read", "--switch", params.host_name, timeout=60
        )
        groups = (observed.get("switches") or {}).get(params.host_name, {}).get(
            "groups"
        ) or []
        got = next(
            (
                g
                for g in groups
                if int(g.get("group_id") or 0) == int(target.get("group_id") or -1)
            ),
            None,
        )
        missing = got is not None and int(target.get("member_id") or -1) not in [
            int(m) for m in (got.get("member_ids") or [])
        ]
        remaining = bool(got and got.get("member_ids"))
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=missing and remaining,
            details={
                "host": params.host_name,
                "group": got,
                "target": target,
            },
        )

    def oracle_playbook(self, params: P4EcmpGroupMemberMissingParams) -> OraclePlaybook:
        switch = params.host_name
        prefix, group_id, member_id, peer = _ecmp_target(
            declared_intent(self.net_env), switch
        )

        return OraclePlaybook(
            finding=(
                f"ECMP group {group_id} ({prefix}) on {switch} lacks member "
                f"{member_id} (next hop {peer}) although the member is still "
                "programmed, so its traffic shares the remaining paths."
            ),
            steps=[
                intent_drift(switch, "groups"),
                switch_state(
                    switch,
                    f"Read the ActionSelector groups and members on {switch}.",
                    group_has(group_id, member_id, present=False),
                    member_defined(member_id),
                    mismatch(switch, "groups"),
                ),
            ],
        )


class P4RuntimePipelineMismatchParams(BaseModel):
    host_name: str = Field(description="Target BMv2 switch name.")


class P4RuntimePipelineMismatch(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    symptom_probe = "path_ping"
    symptom_class = "unreachable"
    root_cause_name = "p4runtime_pipeline_mismatch"
    description = "Loaded P4 pipeline does not match the intended program."
    TAGS = ["p4", "p4_runtime"]
    Params = P4RuntimePipelineMismatchParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        if ctx.scenario == "p4_dc_fabric":
            observer = ctx.net_env.model.client_endpoints()[0]
            return {"host_name": f"leaf_{observer.leaf_id}"}
        if ctx.scenario == "p4_dc_gateway":
            return {
                "host_name": prefer_prefixed_node(
                    ctx.bmv2,
                    prefix="gateway_",
                    preferred="gateway_1",
                    fallback="gateway_1",
                )
            }
        return {"host_name": choice(ctx.rng, bmv2_leaves(ctx.bmv2), "leaf_1")}

    def root_cause_resources(self, params: P4RuntimePipelineMismatchParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: P4RuntimePipelineMismatchParams):
        p4info, json_path = load_blackhole_pipeline(
            self.runtime, params.host_name, _BLACKHOLE_P4
        )
        try:
            result = p4runtime_write(
                self.runtime,
                params.host_name,
                [],
                pipeline={"p4info": p4info, "json": json_path},
                timeout=60,
            )
        finally:
            remove_staged_pipeline(self.runtime, p4info, json_path)
        set_error = result.get("set_error")
        if set_error:
            raise RuntimeError(
                f"pipeline push failed on {params.host_name}: {set_error}"
            )
        logger.info("Loaded mismatched pipeline on %s: %s", params.host_name, result)

    def verify_fault(self, params: P4RuntimePipelineMismatchParams) -> dict:
        observed = run_manager(
            self.runtime, "read", "--switch", params.host_name, timeout=60
        )
        switch = (observed.get("switches") or {}).get(params.host_name) or {}
        pipeline = switch.get("pipeline") or {}
        # A readable device whose loaded P4Info differs from the intended one.
        mismatched = (
            bool(pipeline.get("ok")) and pipeline.get("matches_intent") is False
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=mismatched,
            details={
                "host": params.host_name,
                "pipeline": pipeline,
                "mismatched": mismatched,
            },
        )

    def oracle_playbook(
        self, params: P4RuntimePipelineMismatchParams
    ) -> OraclePlaybook:
        switch = params.host_name
        return OraclePlaybook(
            finding=(
                f"{switch} runs a forwarding pipeline whose P4Info lacks the "
                "intended tables, so it no longer forwards the fabric's traffic."
            ),
            steps=[
                *scope_ping(declared_intent(self.net_env), switch),
                bmv2_running(switch),
                p4rt(
                    f"read --switch {switch}",
                    f"Read the loaded pipeline on {switch}.",
                    all_of(
                        contains(
                            summary_regex(
                                switch,
                                r'"pipeline_ok":\s*true,\s*'
                                r'"pipeline_matches_intent":\s*false',
                            ),
                            f"{switch} answers P4Runtime but its pipeline does not "
                            "match the intent",
                        ),
                        contains(
                            r'"missing_tables":\s*\[\s*"',
                            "the loaded P4Info lacks intended tables",
                        ),
                    ),
                    evidence=True,
                ),
            ],
        )


class P4RuntimePartialWriteParams(BaseModel):
    host_name: str = Field(description="Target BMv2 switch name.")


def partial_write_routes(intent: dict, switch: str) -> dict:
    """Pick the two routes whose selector groups the partial batch exchanges.

    One multi-path route (the faulted ECMP target) and the switch's first
    locally attached host route. Also returns a second local host to send
    from and an endpoint behind an untouched multi-path route as control.
    """
    state = intent["switches"][switch]
    endpoints = {str(e["name"]): str(e["ip"]) for e in intent["endpoints"]}
    peers = {int(m["member_id"]): str(m["peer"]) for m in state["members"]}
    groups = {
        int(g["group_id"]): [int(m) for m in g["member_ids"]] for g in state["groups"]
    }
    victim_prefix, victim_group, *_ = _ecmp_target(intent, switch)
    local = [
        (str(e["prefix"]), int(e["group_id"]), peers[groups[int(e["group_id"])][0]])
        for e in state["ipv4_lpm"]
        if len(groups[int(e["group_id"])]) == 1
        and peers[groups[int(e["group_id"])][0]] in endpoints
    ]
    if len(local) < 2:
        raise RuntimeError(f"{switch} has fewer than two local host routes")
    (host_prefix, host_group, host), (_, _, sender) = local[:2]

    def covered(prefix: str) -> str:
        net = ipaddress.ip_network(prefix)
        return next(n for n, ip in endpoints.items() if ipaddress.ip_address(ip) in net)

    control_prefix = next(
        str(e["prefix"])
        for e in state["ipv4_lpm"]
        if len(groups[int(e["group_id"])]) > 1 and str(e["prefix"]) != victim_prefix
    )
    victim, control = covered(victim_prefix), covered(control_prefix)
    return {
        "switch": switch,
        "victim_prefix": victim_prefix,
        "victim_group": int(victim_group),
        "victim_members": groups[int(victim_group)],
        "host_prefix": host_prefix,
        "host_group": host_group,
        "host_members": groups[host_group],
        "host": host,
        "host_ip": endpoints[host],
        "sender": sender,
        "victim": victim,
        "victim_ip": endpoints[victim],
        "control": control,
        "control_ip": endpoints[control],
    }


def partial_write_intent(intent: dict, pair: dict) -> dict:
    """The re-derived intent: both routes' selector-group IDs exchanged.

    Group ``victim`` now carries the host route's next hops and vice versa,
    and each route points at the other ID, so forwarding is unchanged.
    """
    updated = copy.deepcopy(intent)
    state = updated["switches"][pair["switch"]]
    members = {
        pair["victim_group"]: pair["host_members"],
        pair["host_group"]: pair["victim_members"],
    }
    for group in state["groups"]:
        if int(group["group_id"]) in members:
            group["member_ids"] = list(members[int(group["group_id"])])
    moved = {
        pair["victim_prefix"]: pair["host_group"],
        pair["host_prefix"]: pair["victim_group"],
    }
    for route in state["ipv4_lpm"]:
        if route["prefix"] in moved:
            route["group_id"] = moved[route["prefix"]]
    return updated


class P4RuntimePartialWrite(ProblemBase):
    """The controller's own batch to a new intent is only partially applied.

    The controller re-derives the selector-group IDs of two routes (e.g. a new
    primary after failover), stores that intent, and sends one
    ``CONTINUE_ON_ERROR`` WriteRequest: both group MODIFYs, then the two
    routes as INSERTs instead of MODIFYs. The device applies the group
    rewrites and rejects the route INSERTs with ALREADY_EXISTS. The
    controller's write log records the per-update status, and the device
    state matches the new intent for the groups but not for the routes, so
    each route forwards to the other route's next hops.
    """

    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name = "p4runtime_partial_write"
    description = "A P4Runtime update was only partially applied."
    TAGS = ["p4", "p4_runtime"]
    Params = P4RuntimePartialWriteParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        if ctx.scenario == "p4_dc_gateway":
            return {"host_name": gateway_backend_leaf(ctx.net_env.model)}
        if ctx.scenario == "p4_dc_fabric":
            return {
                "host_name": prefer_prefixed_node(
                    ctx.bmv2, prefix="leaf_", preferred="leaf_1", fallback="leaf_1"
                )
            }
        return {"host_name": choice(ctx.rng, ctx.bmv2, ctx.host0)}

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._pair: dict | None = None

    def root_cause_resources(self, params: P4RuntimePartialWriteParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: P4RuntimePartialWriteParams):
        from nika.net_env.p4_dc_fabric.fabric_manager.apply import (
            INTENT_PATH,
            _copy_in,
        )

        intent = load_intent(self.runtime)
        pair = partial_write_routes(intent, params.host_name)
        self._pair = pair
        _copy_in(
            self.runtime,
            "fabric_mgr",
            INTENT_PATH,
            json.dumps(partial_write_intent(intent, pair), indent=2).encode(),
        )
        victim, host = pair["victim_group"], pair["host_group"]
        batch = [
            {
                "type": "MODIFY",
                "group": {"group_id": victim, "member_ids": pair["host_members"]},
            },
            {
                "type": "MODIFY",
                "group": {"group_id": host, "member_ids": pair["victim_members"]},
            },
            {"lpm": {"prefix": pair["victim_prefix"], "group_id": host}},
            {"lpm": {"prefix": pair["host_prefix"], "group_id": victim}},
        ]
        result = p4runtime_write(self.runtime, params.host_name, batch, logged=True)
        codes = [status["code"] for status in result["statuses"]]
        if codes[:2] != ["OK", "OK"] or "OK" in codes[2:]:
            raise RuntimeError(
                f"batch on {params.host_name} was not partially applied: {result}"
            )
        logger.info("Partial P4Runtime batch on %s: %s", params.host_name, codes)

    def verify_fault(self, params: P4RuntimePartialWriteParams) -> dict:
        """Read groups and routes: the group rewrites applied, the routes did not."""
        raw = self.recheck_artifact(params)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=bool(raw["present"]),
            details=raw["evidence"],
        )

    def recheck_artifact(self, params: P4RuntimePartialWriteParams) -> dict:
        pair = self._pair or partial_write_routes(
            load_intent(self.runtime), params.host_name
        )
        observed = run_manager(
            self.runtime, "read", "--switch", params.host_name, timeout=30
        )
        switch = (observed.get("switches") or {}).get(params.host_name) or {}
        groups = {
            int(g["group_id"]): sorted(int(m) for m in g["member_ids"])
            for g in switch.get("groups") or []
        }
        routes = {
            e.get("prefix"): int(e["group_id"]) for e in switch.get("ipv4_lpm") or []
        }
        victim, host = pair["victim_group"], pair["host_group"]
        groups_rewritten = groups.get(victim) == sorted(
            pair["host_members"]
        ) and groups.get(host) == sorted(pair["victim_members"])
        routes_unchanged = (
            routes.get(pair["victim_prefix"]) == victim
            and routes.get(pair["host_prefix"]) == host
        )
        artifact_ok = groups_rewritten and routes_unchanged
        return {
            "present": artifact_ok,
            "fault": self.root_cause_name,
            "scope": "artifact",
            "evidence": {
                "host": params.host_name,
                "victim_prefix": pair["victim_prefix"],
                "host_prefix": pair["host_prefix"],
                "groups": {
                    str(victim): groups.get(victim),
                    str(host): groups.get(host),
                },
                "routes": {
                    pair["victim_prefix"]: routes.get(pair["victim_prefix"]),
                    pair["host_prefix"]: routes.get(pair["host_prefix"]),
                },
                "groups_rewritten": groups_rewritten,
                "routes_unchanged": routes_unchanged,
            },
            "error": None if artifact_ok else "fault artifact absent",
        }

    def oracle_playbook(self, params: P4RuntimePartialWriteParams) -> OraclePlaybook:
        switch = params.host_name
        return OraclePlaybook(
            finding=(
                f"The controller's last batch to {switch} was only partly applied: "
                "the selector-group rewrites succeeded but the route updates were "
                "rejected (ALREADY_EXISTS), so the routes point at the exchanged "
                "groups and forward to the wrong next hops."
            ),
            steps=[
                intent_drift(switch, "ipv4_lpm"),
                shell(
                    CONTROLLER,
                    f"grep -n -A6 -E 'Write device=.* failed=[1-9]' {CONTROLLER_LOG} "
                    "| tail -30",
                    "Look for controller writes the device rejected in part.",
                    all_of(
                        contains(
                            rf"Write device={switch} .*failed=[1-9]",
                            f"a write to {switch} has failed updates",
                        ),
                        contains(
                            r"MODIFY action_profile_group .*: OK",
                            "its selector-group updates were applied",
                        ),
                        contains(
                            r"INSERT table_entry ipv4_lpm .*ALREADY_EXISTS",
                            "its route updates were rejected",
                        ),
                    ),
                    evidence=True,
                ),
                p4rt(
                    f"read --switch {switch}",
                    f"Compare {switch}'s live groups and routes with the intent.",
                    contains(
                        summary_regex(switch, r'"mismatches":\s*\[\s*"ipv4_lpm"\s*\]'),
                        "groups and members match the intent; only the routes differ",
                    ),
                    evidence=True,
                ),
            ],
        )


# Pools for the stray host routes that fill the table, tried in order until one
# overlaps nothing the fabric routes.
_HOST_ROUTE_POOLS = ("10.128.0.0/16", "10.192.0.0/16", "172.20.0.0/16")


def _host_route_filler(intent: dict, switch: str, count: int) -> list[str]:
    """``count`` /32 host routes from one tenant-sized pool outside the fabric.

    A run such as ``198.0.0.1, 198.0.1.1, ...`` reads as generated on its own;
    scattered host routes inside one pool look like a host-route leak.
    """
    routed = [
        ipaddress.ip_network(entry["prefix"], strict=False)
        for entry in (
            entry
            for node in (intent.get("switches") or {}).values()
            for entry in node.get("ipv4_lpm") or []
        )
        if entry.get("prefix") and not entry["prefix"].endswith("/0")
    ]
    for pool in map(ipaddress.ip_network, _HOST_ROUTE_POOLS):
        if not any(pool.overlaps(net) for net in routed):
            break
    else:
        raise RuntimeError("no free address pool for host routes")
    usable = [h for h in range(1, pool.num_addresses - 1) if h & 0xFF not in (0, 255)]
    hosts = random.Random(f"{switch}:{pool}").sample(usable, min(count, len(usable)))
    return [f"{pool.network_address + h}/32" for h in sorted(hosts)]


class P4TableResourceExhaustionParams(BaseModel):
    host_name: str = Field(description="Target BMv2 switch name.")


class P4TableResourceExhaustion(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name = "p4_table_resource_exhaustion"
    description = "A P4 table has exhausted its capacity."
    TAGS = ["p4", "p4_runtime"]
    Params = P4TableResourceExhaustionParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"host_name": choice(ctx.rng, bmv2_leaves(ctx.bmv2), "leaf_1")}

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._fill: dict | None = None

    def root_cause_resources(self, params: P4TableResourceExhaustionParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: P4TableResourceExhaustionParams):
        intent = load_intent(self.runtime)
        size = lpm_capacity(intent)
        observed = run_manager(
            self.runtime, "read", "--switch", params.host_name, timeout=60
        )
        state = (observed.get("switches") or {}).get(params.host_name) or {}
        existing = {e.get("prefix") for e in state.get("ipv4_lpm") or []}
        group_id = int(intent["switches"][params.host_name]["ipv4_lpm"][0]["group_id"])
        prefixes = _host_route_filler(
            intent, params.host_name, max(0, size - len(existing))
        )
        result = p4runtime_write(
            self.runtime,
            params.host_name,
            [{"lpm": {"prefix": p, "group_id": group_id}} for p in prefixes],
            timeout=120,
        )
        self._fill = {
            "size": size,
            "added": sum(1 for s in result["statuses"] if s["code"] == "OK"),
            "errors": [s for s in result["statuses"] if s["code"] != "OK"][:3],
        }

    def verify_fault(self, params: P4TableResourceExhaustionParams) -> dict:
        """Read the live LPM table and compare its occupancy with its size."""
        capacity = lpm_capacity(load_intent(self.runtime))
        observed = run_manager(
            self.runtime, "read", "--switch", params.host_name, timeout=60
        )
        switch = (observed.get("switches") or {}).get(params.host_name) or {}
        occupancy = len(switch.get("ipv4_lpm") or [])
        at_cap = bool(switch) and occupancy >= capacity
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=at_cap,
            details={
                "host": params.host_name,
                "capacity": capacity,
                "occupancy": occupancy,
                "at_cap": at_cap,
            },
        )

    def oracle_playbook(
        self, params: P4TableResourceExhaustionParams
    ) -> OraclePlaybook:
        from nika.net_env.p4_dc_fabric.fabric_manager.apply import P4INFO_PATH

        switch = params.host_name
        capacity = lpm_capacity(declared_intent(self.net_env))
        lpm_count = summary_regex(switch, r'"ipv4_lpm":\s*(\d+),')
        return OraclePlaybook(
            finding=(
                f"The ipv4_lpm table on {switch} is full ({capacity} of {capacity} "
                "entries) with host routes the intent does not contain, so no "
                "further route can be installed."
            ),
            steps=[
                intent_drift(switch, "ipv4_lpm"),
                shell(
                    CONTROLLER,
                    f"awk '/alias: \"ipv4_lpm\"/{{f=1}} f&&/size:/{{print; exit}}' "
                    f"{P4INFO_PATH}",
                    "Read the ipv4_lpm table size declared in the pipeline P4Info.",
                    contains(rf"size:\s*{capacity}\b", f"ipv4_lpm holds {capacity}"),
                    evidence=True,
                ),
                p4rt(
                    f"read --switch {switch}",
                    f"Count the live ipv4_lpm entries on {switch}.",
                    all_of(
                        Check(
                            f"{switch} holds at least {capacity} routes",
                            lambda out: (
                                (m := re.search(lpm_count, out, re.IGNORECASE))
                                is not None
                                and int(m.group(1)) >= capacity
                            ),
                        ),
                        mismatch(switch, "ipv4_lpm"),
                    ),
                    evidence=True,
                ),
            ],
        )
