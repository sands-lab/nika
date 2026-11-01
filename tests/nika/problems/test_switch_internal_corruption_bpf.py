"""The privileged BPF loader is labelled so lab teardown can reap it."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nika.problems.forwarding_encapsulation_policy.switch_internal_corruption_bpf import (
    SwitchNamespaceBitflip,
)

pytestmark = pytest.mark.unit


def test_containerlab_loader_is_labelled_for_lab_teardown() -> None:
    runtime = SimpleNamespace(lab_name="min3clos__lab")
    bitflip = SwitchNamespaceBitflip(runtime, "containerlab")
    loader = MagicMock()
    loader.wait.return_value = {"StatusCode": 0}
    loader.logs.return_value = b""
    client = MagicMock()
    client.containers.create.return_value = loader
    wire = MagicMock()
    wire.egress_dev.return_value = "nwaaaaaaaaaaaa"

    with (
        patch(
            "nika.problems.forwarding_encapsulation_policy.switch_internal_corruption_bpf.docker_client",
            return_value=client,
        ),
        patch(
            "nika.problems.forwarding_encapsulation_policy.switch_internal_corruption_bpf.ensure_nika_docker_images"
        ),
        patch.object(bitflip, "_wire", return_value=wire),
    ):
        assert bitflip.attach("spine", "eth1", 42) == "nwaaaaaaaaaaaa"

    kwargs = client.containers.create.call_args.kwargs
    assert kwargs["labels"]["nika.lab_name"] == "min3clos__lab"
    assert kwargs["privileged"] is True
    assert kwargs["network_mode"] == "host"
    loader.remove.assert_called_with(force=True)
