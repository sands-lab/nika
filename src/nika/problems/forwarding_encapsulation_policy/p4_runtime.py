"""P4Runtime / ActionSelector failures for shared P4 forwarding intent."""

from __future__ import annotations

import ipaddress
import json
import shlex
from pathlib import Path

from pydantic import BaseModel, Field

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
from nika.utils.logger import system_logger

logger = system_logger

_BAD_PORT = 31
_BLACKHOLE_P4 = Path(__file__).with_name("blackhole.p4")


class P4ActionSelectorMemberMisconfigParams(BaseModel):
    host_name: str = Field(description="Target BMv2 switch name.")


class P4ActionSelectorMemberMisconfig(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    symptom_probe = "custom"
    symptom_class = "loss"
    root_cause_name = "p4_action_selector_member_misconfig"
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
        result = run_manager(
            self.runtime,
            "modify-member",
            "--switch",
            params.host_name,
            "--member-id",
            str(member_id),
            "--port",
            str(_BAD_PORT),
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


class P4EcmpGroupMemberMissingParams(BaseModel):
    host_name: str = Field(description="Target BMv2 switch name.")


class P4EcmpGroupMemberMissing(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    symptom_probe = "custom"
    symptom_class = "degradation"
    root_cause_name = "p4_ecmp_group_member_missing"
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
        result = run_manager(
            self.runtime,
            "delete-group-member",
            "--switch",
            params.host_name,
            "--group-id",
            str(group_id),
            "--member-id",
            str(member_id),
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
        result = run_manager(
            self.runtime,
            "set-pipeline",
            "--switch",
            params.host_name,
            timeout=60,
            p4info=p4info,
            json_path=json_path,
        )
        remove_staged_pipeline(self.runtime, p4info, json_path)
        set_error = result.get("set_error")
        if set_error:
            raise RuntimeError(
                f"set-pipeline failed on {params.host_name}: {set_error}"
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


# One WriteRequest (CONTINUE_ON_ERROR, the P4Runtime default for non-atomic
# devices) from a controller that re-derived its selector-group IDs, e.g. a new
# master after failover: it rewrites both groups in place, then re-sends the
# routes that must follow them as INSERTs. BMv2 applies the group MODIFYs and
# rejects the route INSERTs with ALREADY_EXISTS, so each route is left on a
# group that now holds the other route's next hops.
_PARTIAL_BATCH = r"""
import json, sys
sys.path.insert(0, "/opt/nika")
import grpc
import p4rt_manager as m
from google.rpc import code_pb2, status_pb2
name, plan, intent_path, p4info_path = sys.argv[1:5]
plan = json.loads(plan)
switch = m.load_json(intent_path)["switches"][name]
index = m.P4InfoIndex(m.load_p4info(p4info_path))
updates = [m.group_entity(index, g, insert=False) for g in plan["groups"]]
updates += [m.lpm_entity(index, r, insert=True) for r in plan["routes"]]
client = m.connect(switch)
out = {"codes": ["OK"] * len(updates), "messages": [""] * len(updates)}
try:
    try:
        client.write(updates, m.p4runtime_pb2.WriteRequest.CONTINUE_ON_ERROR)
    except grpc.RpcError as exc:
        details = [
            status_pb2.Status.FromString(value).details
            for key, value in exc.trailing_metadata() or []
            if key == "grpc-status-details-bin"
        ]
        if not details:
            raise
        for i, detail in enumerate(details[0]):
            error = m.p4runtime_pb2.Error()
            detail.Unpack(error)
            out["codes"][i] = code_pb2.Code.Name(error.canonical_code)
            out["messages"][i] = error.message
finally:
    client.close()
print(json.dumps(out))
"""


class P4RuntimePartialWrite(ProblemBase):
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

    def root_cause_resources(self, params: P4RuntimePartialWriteParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: P4RuntimePartialWriteParams):
        from nika.net_env.p4_dc_fabric.fabric_manager.apply import (
            INTENT_PATH,
            P4INFO_PATH,
        )

        pair = partial_write_routes(load_intent(self.runtime), params.host_name)
        victim, host = pair["victim_group"], pair["host_group"]
        plan = {
            "groups": [
                {"group_id": victim, "member_ids": pair["host_members"]},
                {"group_id": host, "member_ids": pair["victim_members"]},
            ],
            "routes": [
                {"prefix": pair["victim_prefix"], "group_id": host},
                {"prefix": pair["host_prefix"], "group_id": victim},
            ],
        }
        out = self.runtime.exec(
            "fabric_mgr",
            f"python3 -c {shlex.quote(_PARTIAL_BATCH)} {params.host_name} "
            f"{shlex.quote(json.dumps(plan))} {INTENT_PATH} {P4INFO_PATH}",
            timeout=60,
        )
        try:
            result = json.loads(out.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"partial batch on {params.host_name}: {out}") from exc
        codes = result["codes"]
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
        pair = partial_write_routes(load_intent(self.runtime), params.host_name)
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
        result = run_manager(
            self.runtime,
            "fill-table",
            "--switch",
            params.host_name,
            "--size",
            str(lpm_capacity(intent)),
            timeout=120,
        )
        self._fill = result

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
