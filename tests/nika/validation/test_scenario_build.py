"""Saved scenario constructions deploy through the existing session lifecycle."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from nika.workflows.validation.scenario_build import load_scenario_build


@pytest.mark.contract
@pytest.mark.parametrize("status", ["failed", "unsupported", None])
def test_saved_build_requires_successful_validation(tmp_path, status):
    (tmp_path / "summary.json").write_text(json.dumps({"status": status}))
    with pytest.raises(ValueError, match="successful validation"):
        load_scenario_build(tmp_path)


@pytest.mark.contract
def test_saved_completion_cannot_fall_back_to_default_parameters(tmp_path):
    specimen = (
        Path(__file__).resolve().parents[3] / "experiment/scenario_validation/clos.json"
    )
    (tmp_path / "summary.json").write_text('{"status": "passed"}')
    (tmp_path / "original-spec.json").write_text(specimen.read_text())
    with pytest.raises(FileNotFoundError, match="routing-parameters.json"):
        load_scenario_build(tmp_path)


@pytest.mark.e2e
def test_saved_build_deploys_with_original_requirements(
    tmp_path, netcomplete_image, batfish_verifier
):
    import time
    from typer.testing import CliRunner
    from nika.cli.main import app
    from nika.net_env.isp.kathara.verify import verify_isp_contract
    from nika.runtime.factory import runtime_for_session
    from nika.utils.session import Session
    from nika.utils.session_store import SessionStore
    from nika.workflows.env.start import start_net_env
    from nika.workflows.session.close import close_session
    from nika.workflows.validation.scenario import validate_scenario_design
    from nika.workflows.validation.scenario_spec import ScenarioExperiment
    from tests.support.scenario_validation import assert_scenario_acceptance

    specimen = (
        Path(__file__).resolve().parents[3] / "experiment/scenario_validation/clos.json"
    )
    spec = ScenarioExperiment.model_validate_json(specimen.read_text())
    build = tmp_path / "construction"
    summary = validate_scenario_design(
        spec, build, image=netcomplete_image, batfish_verifier=batfish_verifier
    )
    assert_scenario_acceptance(spec, summary, build, runtime=False)
    kwargs = load_scenario_build(build)
    assert kwargs["routing_parameters"] == json.loads(
        (build / "routing-parameters.json").read_text()
    )
    session_id = f"routing-reuse-{uuid4().hex[:12]}"
    try:
        start_net_env(
            **kwargs, session_id=session_id, session_dir=str(tmp_path / "replay")
        )
        session = Session().load_running_session(session_id)
        backend = runtime_for_session(session)
        for contract in (spec.healthy, spec.runtime_requirements):
            if contract is None:
                continue
            deadline = time.monotonic() + spec.convergence_timeout_sec
            while True:
                report = verify_isp_contract(backend, contract=contract, plan=None)
                report.write(tmp_path / f"replayed-{contract.contract_id}.json")
                if all(result.status == "passed" for result in report.results):
                    break
                assert time.monotonic() < deadline, report.model_dump()
                time.sleep(3)
        inspected = CliRunner().invoke(
            app,
            [
                "exec",
                "--session_id",
                session_id,
                "leaf_router_0_0",
                "vtysh -c 'show bgp summary json'",
            ],
        )
        assert inspected.exit_code == 0, inspected.output
        assert "Established" in inspected.output
        (tmp_path / "replay-bgp-summary.txt").write_text(inspected.output)
        assert not (tmp_path / "replay/batfish-validation.json").exists()
    finally:
        close_session(session_id=session_id)
    assert session_id not in {
        item["session_id"] for item in SessionStore().list_running_sessions()
    }
    import docker

    client = docker.from_env()
    try:
        assert not any(
            session.lab_name in container.name
            for container in client.containers.list(all=True)
        )
    finally:
        client.close()


@pytest.mark.contract
def test_saved_netlab_build_cannot_fall_back_to_local_renderer(tmp_path):
    specimen = (
        Path(__file__).resolve().parents[3] / "experiment/scenario_validation/clos.json"
    )
    (tmp_path / "summary.json").write_text(
        json.dumps({"status": "passed", "configuration": {"generator": "netlab"}})
    )
    (tmp_path / "original-spec.json").write_text(specimen.read_text())
    (tmp_path / "routing-parameters.json").write_text("{}")
    with pytest.raises(FileNotFoundError, match="missing candidate configurations"):
        load_scenario_build(tmp_path)
