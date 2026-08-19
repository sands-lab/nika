"""P4Runtime / ActionSelector failures for p4_dc_fabric."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from nika.net_env.verify import ping_ok
from nika.problems.problem_base import (
    FailureCause,
    FailureDomain,
    FailureImpact,
    FailureScope,
    FailureSymptom,
    FailureTemporal,
    ProblemBase,
    build_verify_result,
)
from nika.problems.root_cause import node_resource
from nika.utils.logger import system_logger

logger = system_logger

_BAD_PORT = 31
_BLACKHOLE_P4 = Path(__file__).with_name("blackhole.p4")


def _mgr():
    from nika.net_env.kathara.p4.p4_dc_fabric.fabric_manager.apply import (
        load_intent,
        run_manager,
    )

    return load_intent, run_manager


def _load_blackhole_pipeline(runtime, switch: str) -> tuple[str, str]:
    from nika.net_env.kathara.p4.p4_dc_fabric.fabric_manager.apply import (
        FABRIC_DIR,
        compile_pipeline_on_switch,
        _copy_in,
        _copy_out,
    )

    p4info = f"{FABRIC_DIR}/blackhole.p4info.txt"
    json_path = f"{FABRIC_DIR}/blackhole.json"
    _copy_in(runtime, switch, "/tmp/blackhole.p4", _BLACKHOLE_P4.read_bytes())
    compile_pipeline_on_switch(
        runtime, switch, "/tmp/blackhole.p4", "blackhole.p4info.txt", "blackhole.json"
    )
    _copy_in(
        runtime,
        "fabric_mgr",
        json_path,
        _copy_out(runtime, switch, "/tmp/blackhole.json"),
    )
    _copy_in(
        runtime,
        "fabric_mgr",
        p4info,
        _copy_out(runtime, switch, "/tmp/blackhole.p4info.txt"),
    )
    return p4info, json_path


def _ecmp_target(intent: dict, switch: str) -> tuple[str, int, int, str]:
    sw = intent["switches"][switch]
    group = next(g for g in sw["groups"] if g.get("kind") == "ecmp" and g["member_ids"])
    prefix = next(
        e["prefix"]
        for e in sw["ipv4_lpm"]
        if int(e["group_id"]) == int(group["group_id"])
    )
    member_id = int(group["member_ids"][0])
    peer = next(m["peer"] for m in sw["members"] if int(m["member_id"]) == member_id)
    return prefix, int(group["group_id"]), member_id, peer


def _endpoints(net_env):
    model = getattr(net_env, "model", None)
    if model is None:
        raise RuntimeError("p4_dc_fabric model missing on net_env")
    clients = model.client_endpoints()
    webs = model.web_endpoints()
    return model, clients, webs


def _same_rack_ok(runtime, model, leaf_id: int) -> bool:
    web = next(w for w in model.web_endpoints() if w.leaf_id == leaf_id)
    client = next(
        (c for c in model.client_endpoints() if c.leaf_id == leaf_id),
        None,
    )
    if client is None:
        return True
    return ping_ok(runtime, client.name, web.ip)


def _cross_rack_ok(runtime, model, src_leaf: int, dst_leaf: int) -> bool:
    src = next(c for c in model.client_endpoints() if c.leaf_id == src_leaf)
    dst = next(w for w in model.web_endpoints() if w.leaf_id == dst_leaf)
    return ping_ok(runtime, src.name, dst.ip)


class P4ActionSelectorMemberMisconfigParams(BaseModel):
    host_name: str = Field(description="Target BMv2 switch name.")


class P4ActionSelectorMemberMisconfig(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    cause = FailureCause.CONFIGURATION
    symptom = FailureSymptom.MISROUTING
    scope = FailureScope.PATH
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.PARTIAL
    root_cause_name = "p4_action_selector_member_misconfig"
    TAGS = ["p4", "p4_runtime"]
    Params = P4ActionSelectorMemberMisconfigParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._target: dict | None = None

    def root_cause_resources(self, params: P4ActionSelectorMemberMisconfigParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: P4ActionSelectorMemberMisconfigParams):
        load_intent, run_manager = _mgr()
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
        load_intent, run_manager = _mgr()
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
        model, clients, webs = _endpoints(self.net_env)
        leaf_id = (
            int(params.host_name.split("_")[1]) if "leaf_" in params.host_name else 1
        )
        same = _same_rack_ok(self.runtime, model, leaf_id)
        prefix = str(target.get("prefix") or "")
        avoid = -1
        parts = prefix.split(".")
        if prefix.startswith("10.0.") and len(parts) >= 3 and parts[2].isdigit():
            avoid = int(parts[2])
        control_ids = [w.leaf_id for w in webs if w.leaf_id not in {leaf_id, avoid}]
        if "spine_" in params.host_name:
            other = _cross_rack_ok(self.runtime, model, 1, 2)
            same = _same_rack_ok(self.runtime, model, 1)
        elif len(control_ids) >= 2:
            other = _cross_rack_ok(self.runtime, model, control_ids[0], control_ids[1])
        elif control_ids:
            other = _cross_rack_ok(self.runtime, model, leaf_id, control_ids[0])
        else:
            other = True
        verified = member_ok and same and other
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "member": got,
                "target": target,
                "same_rack_ok": same,
                "control_path_ok": other,
            },
        )


class P4EcmpGroupMemberMissingParams(BaseModel):
    host_name: str = Field(description="Target BMv2 switch name.")


class P4EcmpGroupMemberMissing(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    cause = FailureCause.CONFIGURATION
    symptom = FailureSymptom.MISROUTING
    scope = FailureScope.PATH
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.PARTIAL
    root_cause_name = "p4_ecmp_group_member_missing"
    TAGS = ["p4", "p4_runtime"]
    Params = P4EcmpGroupMemberMissingParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._target: dict | None = None

    def root_cause_resources(self, params: P4EcmpGroupMemberMissingParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: P4EcmpGroupMemberMissingParams):
        load_intent, run_manager = _mgr()
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
        load_intent, run_manager = _mgr()
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
        model, _clients, webs = _endpoints(self.net_env)
        leaf_id = 1
        if params.host_name.startswith("leaf_"):
            leaf_id = int(params.host_name.split("_")[1])
        other_leaf = next(w.leaf_id for w in webs if w.leaf_id != leaf_id)
        same = _same_rack_ok(self.runtime, model, leaf_id)
        cross = _cross_rack_ok(self.runtime, model, leaf_id, other_leaf)
        verified = missing and remaining and same and cross
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "group": got,
                "target": target,
                "same_rack_ok": same,
                "cross_rack_ok": cross,
            },
        )


class P4RuntimePipelineMismatchParams(BaseModel):
    host_name: str = Field(description="Target BMv2 switch name.")


class P4RuntimePipelineMismatch(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    cause = FailureCause.CONFIGURATION
    symptom = FailureSymptom.BLACKHOLE
    scope = FailureScope.NODE
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.COMPLETE
    root_cause_name = "p4runtime_pipeline_mismatch"
    TAGS = ["p4", "p4_runtime"]
    Params = P4RuntimePipelineMismatchParams

    def root_cause_resources(self, params: P4RuntimePipelineMismatchParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: P4RuntimePipelineMismatchParams):
        p4info, json_path = _load_blackhole_pipeline(self.runtime, params.host_name)
        _, run_manager = _mgr()
        result = run_manager(
            self.runtime,
            "set-pipeline",
            "--switch",
            params.host_name,
            timeout=60,
            p4info=p4info,
            json_path=json_path,
        )
        logger.info("Loaded mismatched pipeline on %s: %s", params.host_name, result)

    def verify_fault(self, params: P4RuntimePipelineMismatchParams) -> dict:
        _, run_manager = _mgr()
        observed = run_manager(
            self.runtime, "read", "--switch", params.host_name, timeout=60
        )
        switch = (observed.get("switches") or {}).get(params.host_name) or {}
        pipeline = switch.get("pipeline") or {}
        name = str(pipeline.get("pipeline_name") or "")
        mismatched = (
            "fabric" not in name.lower()
            or not (switch.get("ipv4_lpm"))
            or not observed.get("ok", True)
        )
        model, _c, webs = _endpoints(self.net_env)
        leaf_id = (
            int(params.host_name.split("_")[1])
            if params.host_name.startswith("leaf_")
            else 1
        )
        other_leaves = [w.leaf_id for w in webs if w.leaf_id != leaf_id]
        affected = True
        if params.host_name.startswith("leaf_"):
            src = next(c for c in model.client_endpoints() if c.leaf_id == leaf_id)
            dst = next(w for w in webs if w.leaf_id != leaf_id)
            affected = not ping_ok(self.runtime, src.name, dst.ip)
        control = True
        if len(other_leaves) >= 2:
            control = _cross_rack_ok(
                self.runtime, model, other_leaves[0], other_leaves[1]
            )
        elif other_leaves:
            control = _same_rack_ok(self.runtime, model, other_leaves[0])
        verified = mismatched and affected and control
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "pipeline": pipeline,
                "mismatched": mismatched,
                "affected_path_down": affected,
                "control_path_ok": control,
            },
        )


class P4RuntimePartialWriteParams(BaseModel):
    host_name: str = Field(description="Target BMv2 switch name.")


class P4RuntimePartialWrite(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    cause = FailureCause.CONFIGURATION
    symptom = FailureSymptom.BLACKHOLE
    scope = FailureScope.PATH
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.PARTIAL
    root_cause_name = "p4runtime_partial_write"
    TAGS = ["p4", "p4_runtime"]
    Params = P4RuntimePartialWriteParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._prefix: str | None = None

    def root_cause_resources(self, params: P4RuntimePartialWriteParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: P4RuntimePartialWriteParams):
        load_intent, run_manager = _mgr()
        intent = load_intent(self.runtime)
        prefix, *_rest = _ecmp_target(intent, params.host_name)
        self._prefix = prefix
        run_manager(
            self.runtime,
            "partial-write",
            "--switch",
            params.host_name,
            "--prefix",
            prefix,
        )

    def verify_fault(self, params: P4RuntimePartialWriteParams) -> dict:
        load_intent, run_manager = _mgr()
        intent = load_intent(self.runtime)
        prefix = self._prefix or _ecmp_target(intent, params.host_name)[0]
        observed = run_manager(
            self.runtime, "read", "--switch", params.host_name, timeout=60
        )
        entries = (observed.get("switches") or {}).get(params.host_name, {}).get(
            "ipv4_lpm"
        ) or []
        present = any(e.get("prefix") == prefix for e in entries)
        remaining = len(entries)
        model, _c, webs = _endpoints(self.net_env)
        leaf_id = (
            int(params.host_name.split("_")[1])
            if params.host_name.startswith("leaf_")
            else 1
        )
        dst_leaf = int(prefix.split(".")[2]) if prefix.startswith("10.0.") else 2
        src = next(c for c in model.client_endpoints() if c.leaf_id == leaf_id)
        dst = next((w for w in webs if w.leaf_id == dst_leaf), webs[-1])
        affected_down = not ping_ok(self.runtime, src.name, dst.ip)
        same = _same_rack_ok(self.runtime, model, leaf_id)
        verified = (not present) and remaining > 0 and affected_down and same
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "prefix": prefix,
                "present": present,
                "remaining_lpm": remaining,
                "affected_path_down": affected_down,
                "same_rack_ok": same,
            },
        )


class P4TableResourceExhaustionParams(BaseModel):
    host_name: str = Field(description="Target BMv2 switch name.")


class P4TableResourceExhaustion(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    cause = FailureCause.RESOURCE
    symptom = FailureSymptom.LOSS
    scope = FailureScope.NODE
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.PARTIAL
    root_cause_name = "p4_table_resource_exhaustion"
    TAGS = ["p4", "p4_runtime"]
    Params = P4TableResourceExhaustionParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._fill: dict | None = None

    def root_cause_resources(self, params: P4TableResourceExhaustionParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: P4TableResourceExhaustionParams):
        from nika.net_env.kathara.p4.p4_dc_fabric.topology_model import IPV4_LPM_SIZE

        _, run_manager = _mgr()
        result = run_manager(
            self.runtime,
            "fill-table",
            "--switch",
            params.host_name,
            "--size",
            str(IPV4_LPM_SIZE),
            timeout=120,
        )
        self._fill = result

    def verify_fault(self, params: P4TableResourceExhaustionParams) -> dict:
        from nika.net_env.kathara.p4.p4_dc_fabric.topology_model import IPV4_LPM_SIZE

        _, run_manager = _mgr()
        fill = self._fill or run_manager(
            self.runtime,
            "fill-table",
            "--switch",
            params.host_name,
            "--size",
            str(IPV4_LPM_SIZE),
            timeout=120,
        )
        occupancy = int(fill.get("occupancy") or 0)
        at_cap = occupancy >= IPV4_LPM_SIZE
        model, _c, webs = _endpoints(self.net_env)
        leaf_id = (
            int(params.host_name.split("_")[1])
            if params.host_name.startswith("leaf_")
            else 1
        )
        same = _same_rack_ok(self.runtime, model, leaf_id)
        other_leaf = next(w.leaf_id for w in webs if w.leaf_id != leaf_id)
        cross = _cross_rack_ok(self.runtime, model, leaf_id, other_leaf)
        verified = at_cap and same and cross
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "fill": fill,
                "at_cap": at_cap,
                "same_rack_ok": same,
                "cross_rack_ok": cross,
            },
        )
