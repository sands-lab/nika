"""Failure-effect contracts and static/runtime comparison."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nika.net_env.contract import ValidationContract, ValidationReport

FAILURE_EFFECT_FILENAME = "validation-failure-effect.json"
FAULTY_BATFISH_FILENAME = "batfish-validation-faulty.json"
FAULTY_RUNTIME_FILENAME = "validation-results-faulty.json"

EffectStatus = Literal["PASS", "FAIL", "STATIC_RUNTIME_MISMATCH", "UNSUPPORTED"]
BehaviorState = Literal[
    "reachable",
    "unreachable",
    "path_compliant",
    "path_violated",
    "established",
    "not_established",
]


class EffectExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: str = Field(min_length=1)
    from_state: BehaviorState
    to_state: BehaviorState


class FailureEffectContract(BaseModel):
    """Design-time effect declaration for one injected failure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    failure: str = Field(min_length=1)
    expected_change: tuple[EffectExpectation, ...] = ()
    must_preserve: tuple[str, ...] = ()
    supported: bool = True
    reason: str | None = None


class FailureEffectReport(BaseModel):
    """Persisted comparison of healthy/faulty static and runtime behavior."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    failure: str = Field(min_length=1)
    status: EffectStatus
    expected_change: tuple[EffectExpectation, ...] = ()
    must_preserve: tuple[str, ...] = ()
    evidence: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None

    def write(self, path: str) -> None:
        from pathlib import Path
        import json

        Path(path).write_text(
            json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def build_failure_effect_contract(
    problem: Any, baseline: ValidationContract
) -> FailureEffectContract:
    """Build an effect contract from failure-declared class metadata."""
    failure = str(getattr(problem, "root_cause_name", type(problem).__name__))
    protocol = getattr(problem, "effect_protocol", None)
    property_name = getattr(problem, "effect_property", None)
    params = getattr(problem, "_resolved_params", None)
    node = getattr(params, "host_name", None)
    if (protocol is None and property_name is None) or not node:
        return FailureEffectContract(
            failure=failure,
            supported=False,
            reason="failure has no Batfish-modeled effect declaration",
        )

    if property_name == "reachability":
        # Only prefix destinations are broken by a missing advertisement: the
        # failure node stops originating a designed prefix. Endpoint destinations
        # hosted behind the node (e.g. IGP edge stubs) stay reachable, so they
        # must not be declared as expected to change.
        intents = [
            intent
            for intent in baseline.intents
            if intent.property == "reachability"
            and intent.destination is not None
            and intent.destination.kind == "prefix"
            and intent.destination.node == node
        ]
        from_state, to_state = "reachable", "unreachable"
    else:
        intents = [
            intent
            for intent in baseline.intents
            if intent.property == "adjacency"
            and intent.adjacency is not None
            and intent.adjacency.protocol == protocol
            and node in {intent.adjacency.local_node, intent.adjacency.remote_node}
        ]
        from_state, to_state = "established", "not_established"
    if not intents:
        return FailureEffectContract(
            failure=failure,
            supported=False,
            reason=f"no declared {property_name or protocol} effect contains node {node!r}",
        )
    return FailureEffectContract(
        failure=failure,
        expected_change=tuple(
            EffectExpectation(
                intent=intent.id,
                from_state=from_state,
                to_state=to_state,
            )
            for intent in intents
        ),
    )


def compare_failure_effect(
    effect: FailureEffectContract,
    contract: ValidationContract,
    *,
    healthy_static: ValidationReport | None,
    faulty_static: ValidationReport | None,
    healthy_runtime: ValidationReport | None,
    faulty_runtime: ValidationReport | None,
) -> FailureEffectReport:
    """Compare expected changes across Batfish and the live runtime report."""
    if not effect.supported:
        return FailureEffectReport(
            failure=effect.failure,
            status="UNSUPPORTED",
            expected_change=effect.expected_change,
            must_preserve=effect.must_preserve,
            reason=effect.reason,
        )
    if any(report is None for report in (healthy_static, faulty_static)):
        return FailureEffectReport(
            failure=effect.failure,
            status="UNSUPPORTED",
            expected_change=effect.expected_change,
            must_preserve=effect.must_preserve,
            reason="Batfish healthy/faulty reports are required",
        )
    if healthy_runtime is None or faulty_runtime is None:
        return FailureEffectReport(
            failure=effect.failure,
            status="UNSUPPORTED",
            expected_change=effect.expected_change,
            must_preserve=effect.must_preserve,
            reason="runtime healthy/faulty reports are required",
        )

    expectations = list(effect.expected_change)
    preserve_ids = list(effect.must_preserve)
    all_ids = [item.intent for item in expectations] + preserve_ids
    evidence: dict[str, Any] = {"intents": {}}
    mismatch = False
    failure_reason: str | None = None

    for intent_id in all_ids:
        intent = next((item for item in contract.intents if item.id == intent_id), None)
        if intent is None:
            return FailureEffectReport(
                failure=effect.failure,
                status="FAIL",
                expected_change=effect.expected_change,
                must_preserve=effect.must_preserve,
                reason=f"effect references unknown intent {intent_id!r}",
            )
        expected_faulty = next(
            (item.to_state for item in expectations if item.intent == intent_id),
            _intent_state(intent),
        )
        expected_healthy = next(
            (item.from_state for item in expectations if item.intent == intent_id),
            _intent_state(intent),
        )
        observations = {
            "batfish": {
                "healthy": _observation(healthy_static, intent_id, intent),
                "faulty": _observation(faulty_static, intent_id, intent),
            },
            "runtime": {
                "healthy": _observation(healthy_runtime, intent_id, intent),
                "faulty": _observation(faulty_runtime, intent_id, intent),
            },
        }
        evidence["intents"][intent_id] = {
            "expected": {"healthy": expected_healthy, "faulty": expected_faulty},
            **observations,
        }
        states = {
            f"{verifier}_{phase}": observation["state"]
            for verifier, phases in observations.items()
            for phase, observation in phases.items()
        }
        unavailable = {
            key: state
            for key, state in states.items()
            if state in {None, "unsupported", "error"}
        }
        if unavailable:
            return FailureEffectReport(
                failure=effect.failure,
                status="UNSUPPORTED",
                expected_change=effect.expected_change,
                must_preserve=effect.must_preserve,
                evidence=evidence,
                reason=f"required effect evidence is unavailable: {unavailable}",
            )
        if (
            states["batfish_healthy"] != states["runtime_healthy"]
            or states["batfish_faulty"] != states["runtime_faulty"]
        ):
            mismatch = True
        if states["batfish_healthy"] != expected_healthy:
            failure_reason = f"healthy baseline failed for intent {intent_id!r}"
        if states["batfish_faulty"] != expected_faulty:
            failure_reason = f"expected effect not observed for intent {intent_id!r}"

    status: EffectStatus
    if mismatch:
        status = "STATIC_RUNTIME_MISMATCH"
    elif failure_reason is not None:
        status = "FAIL"
    else:
        status = "PASS"
    return FailureEffectReport(
        failure=effect.failure,
        status=status,
        expected_change=effect.expected_change,
        must_preserve=effect.must_preserve,
        evidence=evidence,
        reason=failure_reason,
    )


def _intent_state(intent: Any) -> BehaviorState:
    if intent.property == "adjacency":
        return "established"
    if intent.property == "waypoint":
        return "path_compliant"
    return intent.expected


def _state(report: ValidationReport, intent_id: str, intent: Any) -> str | None:
    row = next((item for item in report.results if item.intent == intent_id), None)
    if row is None:
        return None
    if row.status == "unsupported":
        return "unsupported"
    if row.status == "error":
        return "error"
    if row.status == "passed":
        return _intent_state(intent)
    if intent.property == "adjacency":
        return "not_established"
    if intent.property == "waypoint":
        return "path_violated"
    return "unreachable" if intent.expected == "reachable" else "reachable"


def _observation(
    report: ValidationReport, intent_id: str, intent: Any
) -> dict[str, Any]:
    row = next((item for item in report.results if item.intent == intent_id), None)
    if row is None:
        return {
            "state": None,
            "status": "missing",
            "evidence": {},
            "reason": "intent result is missing",
            "duration_ms": None,
        }
    return {
        "state": _state(report, intent_id, intent),
        "status": row.status,
        "evidence": row.evidence,
        "reason": row.reason,
        "duration_ms": row.duration_ms,
    }


def run_failure_effect_validation(
    *, problem: Any, contract: ValidationContract, artifact_dir: str
) -> FailureEffectReport:
    """Validate one injected ISP failure against static and runtime evidence."""
    from pathlib import Path

    root = Path(artifact_dir)
    effect = build_failure_effect_contract(problem, contract)
    healthy_static = _load_report(root / "batfish-validation.json")
    healthy_runtime = _load_report(root / "validation-results.json")
    faulty_static: ValidationReport | None = None
    faulty_runtime: ValidationReport | None = None

    if effect.supported:
        faulty_runtime = _faulty_runtime_report(problem, contract)
        if faulty_runtime is not None:
            faulty_runtime.write(str(root / FAULTY_RUNTIME_FILENAME))
        if healthy_static is not None:
            faulty_static = _faulty_batfish_report(problem, contract, root)

    report = compare_failure_effect(
        effect,
        contract,
        healthy_static=healthy_static,
        faulty_static=faulty_static,
        healthy_runtime=healthy_runtime,
        faulty_runtime=faulty_runtime,
    )
    report.write(str(root / FAILURE_EFFECT_FILENAME))
    return report


def _load_report(path: Any) -> ValidationReport | None:
    from pathlib import Path

    target = Path(path)
    return ValidationReport.load(target) if target.is_file() else None


def _faulty_runtime_report(
    problem: Any, contract: ValidationContract
) -> ValidationReport | None:
    if getattr(problem, "lab_backend", None) != "kathara":
        return None
    from nika.workflows.benchmark.isp_options import is_isp_scenario

    if not is_isp_scenario(contract.scenario):
        return None
    from nika.net_env.isp.kathara.verify import verify_isp_contract

    return verify_isp_contract(
        problem.runtime, contract=contract, plan=problem.net_env.plan
    )


def _faulty_batfish_report(
    problem: Any, contract: ValidationContract, root: Any
) -> ValidationReport | None:
    from nika.workflows.benchmark.isp_options import is_isp_scenario

    if getattr(problem, "lab_backend", None) != "kathara" or not is_isp_scenario(
        contract.scenario
    ):
        return None
    from nika.validation.batfish.service import ensure_batfish_service
    from nika.validation.batfish.snapshot import build_isp_snapshot
    from nika.validation.batfish.verifier import BatfishVerifier

    env = problem.net_env
    configs = dict(env.deployment_configs)
    for node in env.plan.nodes:
        content = problem.runtime.exec(node.device_name, "cat /etc/frr/frr.conf")
        if not content or str(content).startswith("[TIMEOUT]"):
            return None
        configs[node.device_name] = str(content)
    snapshot = build_isp_snapshot(
        root=root / "failure-effect",
        contract=contract,
        plan=env.plan,
        traffic=env.traffic,
        deployment_configs=configs,
    )
    ensure_batfish_service()
    report = BatfishVerifier().verify(contract, snapshot)
    report.write(root / FAULTY_BATFISH_FILENAME)
    return report
