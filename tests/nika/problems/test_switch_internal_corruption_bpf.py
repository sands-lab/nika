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
    runtime = SimpleNamespace(lab_name="dc_clos__lab")
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
        bitflip.attach("spine", "eth1", 42)

    kwargs = client.containers.create.call_args.kwargs
    assert kwargs["labels"]["nika.lab_name"] == "dc_clos__lab"
    assert kwargs["privileged"] is True
    assert kwargs["network_mode"] == "host"
    loader.remove.assert_called_with(force=True)


def test_kathara_classifier_uses_proxy_namespace_and_recovery_removes_proxy() -> None:
    runtime = SimpleNamespace(lab_name="dc_clos__lab")
    bitflip = SwitchNamespaceBitflip(runtime)
    proxy = MagicMock()
    state = SimpleNamespace(proxy_id="proxy-id")
    proxy.insert.return_value = state
    proxy.discover.return_value = state
    with (
        patch.object(bitflip, "_proxy", return_value=proxy),
        patch.object(bitflip, "_load") as load,
    ):
        bitflip.attach("spine", "eth1", 42)
        proxy.insert.assert_called_once_with("spine", "eth1", allow_shared=True)
        load.assert_called_once_with(
            "eth1",
            42,
            frozenset(),
            network_mode="container:proxy-id",
            qdisc=True,
        )
        bitflip.detach("spine", "eth1")
        proxy.remove.assert_called_once_with(state)


@pytest.mark.parametrize(
    "error", [RuntimeError("load failed"), KeyboardInterrupt("load failed")]
)
def test_failed_kathara_load_resumes_hub_and_removes_proxy(error) -> None:
    bitflip = SwitchNamespaceBitflip(SimpleNamespace(lab_name="dc_clos__lab"))
    proxy = MagicMock()
    with (
        patch.object(bitflip, "_proxy", return_value=proxy),
        patch.object(bitflip, "_load", side_effect=error),
    ):
        with pytest.raises(type(error), match="load failed"):
            bitflip.attach("spine", "eth1", 42)
    proxy.remove.assert_called_once_with(proxy.insert.return_value)
