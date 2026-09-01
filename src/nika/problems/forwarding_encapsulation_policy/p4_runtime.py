"""P4Runtime / ActionSelector failures for shared P4 forwarding intent."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from nika.problems.base import (
    FailureDomain,
    ProblemBase,
    build_verify_result,
)
from nika.problems.rca import node_resource
from nika.net_env.verify import http_ok
from nika.problems.forwarding_encapsulation_policy.p4runtime_helpers import (
    ecmp_target as _ecmp_target,
    load_blackhole_pipeline,
    load_intent,
    lpm_capacity,
    run_manager,
)
from nika.problems.support.probe_paths import get_probe_path
from nika.utils.logger import system_logger

logger = system_logger

_BAD_PORT = 31
_BLACKHOLE_P4 = Path(__file__).with_name("blackhole.p4")


class P4ActionSelectorMemberMisconfigParams(BaseModel):
    host_name: str = Field(description="Target BMv2 switch name.")


class P4ActionSelectorMemberMisconfig(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name = "p4_action_selector_member_misconfig"
    description = "A P4 ActionSelector member is misconfigured."
    TAGS = ["p4", "p4_runtime"]
    Params = P4ActionSelectorMemberMisconfigParams

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
    root_cause_name = "p4_ecmp_group_member_missing"
    description = "A member is missing from a P4 ECMP group."
    TAGS = ["p4", "p4_runtime"]
    Params = P4EcmpGroupMemberMissingParams

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
    root_cause_name = "p4runtime_pipeline_mismatch"
    description = "Loaded P4 pipeline does not match the intended program."
    TAGS = ["p4", "p4_runtime"]
    Params = P4RuntimePipelineMismatchParams

    def _expected_pipeline_name(self) -> str:
        if self.scenario_name == "p4_dc_gateway":
            from nika.net_env.p4_dc_gateway.topology_model import PIPELINE_NAME

            return PIPELINE_NAME
        from nika.net_env.p4_dc_fabric.topology_model import PIPELINE_NAME

        return PIPELINE_NAME

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
        name = str(pipeline.get("pipeline_name") or "").lower()
        expected = self._expected_pipeline_name().lower()
        mismatched = (
            name != expected
            or not (switch.get("ipv4_lpm"))
            or not observed.get("ok", True)
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


class P4RuntimePartialWrite(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name = "p4runtime_partial_write"
    description = "A P4Runtime update was only partially applied."
    TAGS = ["p4", "p4_runtime"]
    Params = P4RuntimePartialWriteParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._prefix: str | None = None

    def root_cause_resources(self, params: P4RuntimePartialWriteParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: P4RuntimePartialWriteParams):
        intent = load_intent(self.runtime)
        prefix, *_rest = _ecmp_target(intent, params.host_name)
        self._prefix = prefix
        result = run_manager(
            self.runtime,
            "partial-write",
            "--switch",
            params.host_name,
            "--prefix",
            prefix,
        )
        if not result.get("ok"):
            raise RuntimeError(
                f"partial-write failed on {params.host_name} prefix {prefix}: {result}"
            )

    def _light_http_symptom(self) -> tuple[bool, dict]:
        topo_size = getattr(self.net_env, "topo_size", None) or "s"
        path = get_probe_path(self.scenario_name or "", topo_size=str(topo_size))
        if path is None or not path.http_url:
            return True, {"skipped": True, "reason": "no_probe_path"}
        http_ok_val = http_ok(self.runtime, path.src_host, path.http_url)
        return (not http_ok_val), {
            "observer": path.src_host,
            "http_url": path.http_url,
            "http_ok": http_ok_val,
        }

    def verify_fault(self, params: P4RuntimePartialWriteParams) -> dict:
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
        artifact_ok = (not present) and remaining > 0
        symptom_ok, symptom_details = self._light_http_symptom()
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=artifact_ok and symptom_ok,
            details={
                "artifact": {
                    "verified": artifact_ok,
                    "host": params.host_name,
                    "prefix": prefix,
                    "present": present,
                    "remaining_lpm": remaining,
                },
                "symptom": {"verified": symptom_ok, **symptom_details},
            },
        )


class P4TableResourceExhaustionParams(BaseModel):
    host_name: str = Field(description="Target BMv2 switch name.")


class P4TableResourceExhaustion(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name = "p4_table_resource_exhaustion"
    description = "A P4 table has exhausted its capacity."
    TAGS = ["p4", "p4_runtime"]
    Params = P4TableResourceExhaustionParams

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
        intent = load_intent(self.runtime)
        capacity = lpm_capacity(intent)
        fill = self._fill or run_manager(
            self.runtime,
            "fill-table",
            "--switch",
            params.host_name,
            "--size",
            str(capacity),
            timeout=120,
        )
        occupancy = int(fill.get("occupancy") or 0)
        at_cap = occupancy >= capacity
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=at_cap,
            details={
                "host": params.host_name,
                "fill": fill,
                "at_cap": at_cap,
            },
        )
