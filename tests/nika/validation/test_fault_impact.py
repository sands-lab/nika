"""Fault acceptance uses original intent bounds, independently of model predictions."""

import pytest
from pathlib import Path

from nika.net_env.contract import (
    ValidationReport,
    ValidationResult,
    ValidationSanityResult,
)
from nika.workflows.validation.impact import assess_fault_impact


def report(rows, *, sanity=()):
    return ValidationReport(
        contract_id="fault",
        verifier="test",
        status="failed",
        results=tuple(
            ValidationResult(
                intent=intent,
                verifier="test",
                status=status,
                supported=status != "unsupported",
                duration_ms=0,
            )
            for intent, status in rows
        ),
        sanity=sanity,
    )


@pytest.mark.unit
@pytest.mark.parametrize("optional_status", ["passed", "failed"])
def test_fault_bounds_allow_only_authored_optional_impact(optional_status):
    result = assess_fault_impact(
        [
            report(
                [
                    ("session", "failed"),
                    ("service", "passed"),
                    ("exit", optional_status),
                ]
            )
        ],
        intent_ids=["session", "service", "exit"],
        expected_failed_ids=["session"],
        unconstrained_ids=["exit"],
    )
    assert result["status"] == "passed"
    assert result["must_preserve_ids"] == ["service"]


@pytest.mark.unit
def test_fault_rejects_missing_symptom_and_collateral_damage():
    result = assess_fault_impact(
        [report([("session", "passed"), ("service", "failed")])],
        intent_ids=["session", "service"],
        expected_failed_ids=["session"],
    )
    assert result["status"] == "failed"
    assert result["violations"]["missing_required_failures"] == ["session"]
    assert result["violations"]["unexpected_failures"] == ["service"]


@pytest.mark.unit
@pytest.mark.parametrize("status", ["unsupported", "error", "skipped"])
def test_unconstrained_outcomes_still_require_observation(status):
    result = assess_fault_impact(
        [report([("session", "failed"), ("exit", status)])],
        intent_ids=["session", "exit"],
        expected_failed_ids=["session"],
        unconstrained_ids=["exit"],
    )
    assert result["status"] == "failed"
    assert result["violations"]["unresolved_ids"] == ["exit"]


@pytest.mark.unit
def test_fault_acceptance_requires_complete_unique_scope_and_sane_analysis():
    result = assess_fault_impact(
        [
            report(
                [("session", "failed"), ("session", "failed"), ("unknown", "passed")],
                sanity=[
                    ValidationSanityResult(
                        check="parse", status="failed", duration_ms=0
                    )
                ],
            )
        ],
        intent_ids=["session", "service"],
        expected_failed_ids=["session"],
    )
    assert result["status"] == "failed"
    assert result["violations"]["missing_ids"] == ["service"]
    assert result["violations"]["unknown_ids"] == ["unknown"]
    assert result["violations"]["duplicate_ids"] == ["session"]
    assert result["violations"]["sanity_failures"][0]["check"] == "parse"


@pytest.mark.unit
def test_static_scope_does_not_claim_runtime_only_fault_coverage():
    result = assess_fault_impact(
        [report([("session", "failed")])],
        intent_ids=["session"],
        expected_failed_ids=["session", "packet"],
    )
    assert result["status"] == "passed"
    assert result["must_fail_ids"] == ["session"]


@pytest.mark.e2e
def test_modeled_withdrawal_and_native_ecmp_use_original_fault_bounds(
    tmp_path, netcomplete_image, batfish_verifier
):
    from nika.workflows.validation.scenario import validate_scenario_design
    from nika.workflows.validation.scenario_spec import ScenarioExperiment
    from tests.support.scenario_validation import assert_scenario_acceptance

    specimen = (
        Path(__file__).resolve().parents[3]
        / "tests/fixtures/scenario_validation/clos.json"
    )
    spec = ScenarioExperiment.model_validate_json(specimen.read_text())
    withdrawal = spec.failure_checks[0].model_copy(
        update={"static_edits": {"leaf_router_0_1": (" network 10.0.1.0/24\n", "")}}
    )
    spec = spec.model_copy(update={"failure_checks": (withdrawal,)})
    summary = validate_scenario_design(
        spec,
        tmp_path,
        runtime=True,
        image=netcomplete_image,
        batfish_verifier=batfish_verifier,
    )
    assert_scenario_acceptance(spec, summary, tmp_path, runtime=True)
    model = summary["failure_models"][withdrawal.name]["impact"]
    assert set(model["must_fail_ids"]) == {"reach.leaf0.leaf1", "reach.leaf1.leaf0"}
    observed = summary["failure_compatibility"][withdrawal.name]
    assert set(observed["failed_ids"]) == set(withdrawal.expected_failed_ids)
    assert observed["model_comparison"]["only_model_failed_ids"] == []
    assert observed["model_comparison"]["only_runtime_failed_ids"] == []
