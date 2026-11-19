"""One routing completion workflow binds native vendor configuration syntax."""

import pytest

from tests.support.scenario_validation import assert_scenario_acceptance

from nika.workflows.validation.scenario import validate_scenario_design


@pytest.mark.e2e
@pytest.mark.parametrize("specimen", ["clos", "campus", "enterprise"])
def test_routeros_routers_complete_execute_and_restore(
    specimen, tmp_path, netcomplete_image, batfish_verifier
):
    """RouterOS routers in Ethernet environments follow the FRR specimen sequence."""
    from pathlib import Path

    import docker
    from docker.errors import ImageNotFound

    from nika.net_env.utils.routeros.common import IMAGE
    from nika.workflows.validation.scenario_spec import ScenarioExperiment

    client = docker.from_env()
    try:
        client.images.get(IMAGE)
    except ImageNotFound:
        pytest.skip(f"Native vendor image unavailable: {IMAGE}")
    finally:
        client.close()
    specimens = (
        Path(__file__).resolve().parents[3] / "tests/fixtures/scenario_validation"
    )
    spec = ScenarioExperiment.model_validate_json(
        (specimens / f"{specimen}-routeros.json").read_text()
    )
    summary = validate_scenario_design(
        spec,
        tmp_path,
        runtime=True,
        image=netcomplete_image,
        batfish_verifier=batfish_verifier,
    )
    assert_scenario_acceptance(spec, summary, tmp_path, runtime=True)
    assert summary["synthesis"]["backend"] == "netcomplete"
