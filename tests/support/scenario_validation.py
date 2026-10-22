"""Acceptance assertions shared by routing and packet-forwarding experiments."""

import json
from pathlib import Path

from nika.net_env.contract import ValidationReport
from nika.utils.session_store import SessionStore
from nika.workflows.validation.scenario_spec import ScenarioExperiment


def assert_scenario_acceptance(
    spec: ScenarioExperiment, summary: dict, output: Path, *, runtime: bool
) -> None:
    """Check original predicate coverage, operating states and completed cleanup."""
    saved = json.loads((output / "summary.json").read_text())
    assert saved["status"] == summary["status"] == "passed"
    assert (
        saved["validation_scope"]
        == summary["validation_scope"]
        == ("static-and-runtime" if runtime else "static-only")
    )
    original_states = {"healthy": spec.healthy}
    original_states.update((i.name, i.requirements) for i in spec.incidents)
    resolved = output / "resolved-spec.json"
    if resolved.exists():
        spec = ScenarioExperiment.model_validate_json(resolved.read_text())
    states = {"healthy": spec.healthy}
    states.update((i.name, i.requirements) for i in spec.incidents)
    for name, contract in original_states.items():
        resolved_intents = {intent.id: intent for intent in states[name].intents}
        assert all(resolved_intents.get(i.id) == i for i in contract.intents), name
    contracts = [*states.values()]
    contracts.extend(
        c
        for c in [
            spec.runtime_requirements,
            *(i.runtime_requirements for i in spec.incidents),
            *(f.requirements for f in spec.failure_checks),
        ]
        if c is not None
    )

    def check_report(path, contract=None):
        report = ValidationReport.model_validate_json(path.read_text())
        declared = [contract] if contract is not None else contracts
        assert any(
            report.contract_id == c.contract_id
            and {r.intent for r in report.results} == {i.id for i in c.intents}
            and len(report.results) == len(c.intents)
            for c in declared
        ), path
        assert report.results and report.status == "passed", path
        assert all(r.status == "passed" for r in (*report.results, *report.sanity)), (
            path
        )
        return report

    for name, contract in states.items():
        check_report(output / f"static-{name}.json", contract)
    for name, result in summary["counterexamples"].items():
        assert result["status"] in {"rejected", "rejected-in-runtime"}, name
        assert result["failed_ids"], name
        if "runtime_status" in result:
            assert runtime and result["runtime_status"] == "rejected", name
    for mutations, expected_status in (
        (spec.counterexamples, "rejected"),
        (spec.runtime_counterexamples if runtime else (), "rejected-in-runtime"),
    ):
        for mutation in mutations:
            result = summary["counterexamples"][mutation.name]
            assert result["status"] == expected_status
            assert set(result["failed_ids"]) == set(mutation.expected_failed_ids)
    if not runtime:
        assert not summary["runtime"]
        return
    runtime_states = dict(states)
    if spec.runtime_requirements:
        runtime_states["healthy-additional"] = spec.runtime_requirements
    for incident in spec.incidents:
        if incident.runtime_requirements:
            runtime_states[incident.name + "-additional"] = (
                incident.runtime_requirements
            )
    for failure in spec.failure_checks:
        if failure.requirements:
            runtime_states["failure-" + failure.name + "-state"] = failure.requirements
    recoveries = [i.name + "-recovered" for i in spec.incidents]
    recoveries += [m.name + "-recovered" for m in spec.runtime_counterexamples]
    recoveries += [f.name + "-restored" for f in spec.failure_checks]
    for name in recoveries:
        runtime_states[name] = spec.healthy
        if spec.runtime_requirements:
            runtime_states[name + "-additional"] = spec.runtime_requirements
    assert runtime_states.keys() <= summary["runtime"].keys()
    assert set(summary.get("failure_compatibility", {})) == {
        failure.name for failure in spec.failure_checks
    }
    for failure in spec.failure_checks:
        result = summary["failure_compatibility"][failure.name]
        assert result["problem"] == failure.problem
        expected = set(failure.expected_failed_ids)
        if failure.static_edits:
            model = ValidationReport.model_validate_json(
                (output / f"model-{failure.name}.json").read_text()
            )
            predicted = {r.intent for r in model.results if r.status == "failed"}
            assert expected <= predicted
            expected = predicted
        assert set(result["failed_ids"]) == expected
    for name in summary["runtime"]:
        report = check_report(output / f"runtime-{name}.json", runtime_states.get(name))
        assert summary["runtime"][name]["passed"] == len(report.results), name
    run = json.loads((output / "runtime" / "run.json").read_text())
    assert run["status"] == "finished"
    assert run["session_id"] not in {
        s["session_id"] for s in SessionStore().list_running_sessions()
    }
