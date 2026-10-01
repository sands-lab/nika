"""Real full audit of one small case.

The test deploys its own lab and undeploys that lab in ``audit_case``.
It stays idle while the host is already running other labs.
"""

from __future__ import annotations

import subprocess

import pytest

from nika.audit.environment import admits
from nika.workflows.benchmark.inject_resolve import resolve_inject_params
from tests.audit.live import audit_case
from tests.support.prerequisites import docker_available

pytestmark = pytest.mark.e2e


def _running_containers() -> int:
    if not docker_available():
        return 0
    output = subprocess.check_output(["docker", "ps", "-q"], text=True)
    return len([line for line in output.splitlines() if line.strip()])


@pytest.mark.skipif(not docker_available(), reason="Docker is required")
@pytest.mark.skipif(
    _running_containers() > 0,
    reason="host already runs labs; this audit would add another lab",
)
def test_dc_clos_link_down_observes_path_and_cleans_up() -> None:
    inject = resolve_inject_params("link_down", "dc_clos", "s", seed=1)
    before = set(_container_names())
    report = audit_case(
        {
            "scenario": "dc_clos",
            "problem": "link_down",
            "topo_size": "s",
            "inject": inject,
        }
    )
    after = set(_container_names())
    stages = {stage.stage: stage.status for stage in report.stages}
    assert stages["baseline_lab"] == "pass"
    assert stages["baseline_path"] == "pass"
    assert stages["inject_artifact"] == "pass"
    assert stages["symptom"] == "pass"
    assert stages["persistence_artifact"] == "pass"
    assert stages["persistence_symptom"] == "pass"
    assert stages["final_artifact"] == "pass"
    assert stages["final_symptom"] == "pass"
    if stages["control_path"] == "pass":
        assert report.admission() == "pass"
        assert admits(report.admission()) is True
    else:
        assert stages["control_path"] == "unsupported"
        assert report.admission() == "pass"
        assert admits(report.admission()) is True
    assert after <= before


def _container_names() -> list[str]:
    output = subprocess.check_output(
        ["docker", "ps", "--format", "{{.Names}}"], text=True
    )
    return [line.strip() for line in output.splitlines() if line.strip()]
