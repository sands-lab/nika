"""Container lookup must tolerate untagged Docker images."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from Kathara.exceptions import MachineNotFoundError
from nika.service.kathara import docker_utils

pytestmark = pytest.mark.unit


def test_lookup_uses_scoped_api_without_image_tag_or_stats(monkeypatch):
    container = SimpleNamespace(image=SimpleNamespace(tags=[]))
    manager = Mock()
    manager.get_machine_api_object.return_value = container
    monkeypatch.setattr(docker_utils.Kathara, "get_instance", lambda: manager)
    assert (
        docker_utils.get_machine_container(lab_name="owned", host_name="router")
        is container
    )
    manager.get_machine_api_object.assert_called_once_with(
        machine_name="router", lab_name="owned"
    )
    manager.get_machine_stats.assert_not_called()


def test_missing_container_keeps_value_error_contract(monkeypatch):
    manager = Mock()
    manager.get_machine_api_object.side_effect = MachineNotFoundError("missing")
    monkeypatch.setattr(docker_utils.Kathara, "get_instance", lambda: manager)
    with pytest.raises(ValueError, match="No container found"):
        docker_utils.get_machine_container(lab_name="owned", host_name="router")
