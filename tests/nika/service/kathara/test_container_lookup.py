"""Resolve Kathara containers without inspecting their image tags."""

from unittest.mock import Mock

import pytest
from Kathara.exceptions import MachineNotFoundError
from Kathara.manager.docker.stats.DockerMachineStats import DockerMachineStats

from nika.service.kathara import docker_utils

pytestmark = pytest.mark.unit


def test_lookup_accepts_a_container_with_an_untagged_image(monkeypatch):
    container = Mock()
    container.labels = {"lab_hash": "lab", "name": "router", "user": "test"}
    container.name = "router"
    container.image.tags = []
    manager = Mock()
    manager.get_machine_stats.side_effect = lambda **kwargs: iter(
        [DockerMachineStats(container)]
    )
    manager.get_machine_api_object.return_value = container
    monkeypatch.setattr(docker_utils.Kathara, "get_instance", lambda: manager)

    assert (
        docker_utils.get_machine_container(lab_name="lab", host_name="router")
        is container
    )
    manager.get_machine_api_object.assert_called_once_with(
        machine_name="router", lab_name="lab"
    )


def test_missing_container_raises_value_error(monkeypatch):
    manager = Mock()
    manager.get_machine_api_object.side_effect = MachineNotFoundError("missing")
    monkeypatch.setattr(docker_utils.Kathara, "get_instance", lambda: manager)

    with pytest.raises(ValueError, match="No container found"):
        docker_utils.get_machine_container(lab_name="lab", host_name="router")
