from types import SimpleNamespace
from unittest.mock import Mock, patch

from nika.problems.link_interface.link import (
    LinkFailure,
    LinkFailureParams,
    LinkFlap,
    LinkFlapParams,
)


def test_containerlab_link_down_uses_controller_not_runtime_interface_api() -> None:
    problem = LinkFailure(None)
    problem.runtime = SimpleNamespace(backend="containerlab", exec=Mock())
    params = LinkFailureParams(host_name="leaf1", intf_name="e1-1")

    with patch(
        "nika.problems.link_interface.link.HostTcController"
    ) as controller_class:
        controller = controller_class.return_value
        controller.set_netem_loss.return_value = "veth123"

        problem.inject_fault(params)

    problem.runtime.exec.assert_not_called()
    assert not hasattr(problem.runtime, "set_interface_state")
    controller.set_netem_loss.assert_called_once_with("leaf1", "e1-1", 100)


def test_kathara_link_down_uses_hidden_vde_proxy_not_runtime_interface_api() -> None:
    problem = LinkFailure(None)
    problem.runtime = SimpleNamespace(backend="kathara", exec=Mock())
    params = LinkFailureParams(host_name="router1", intf_name="eth0")

    with patch(
        "nika.problems.link_interface.link.KatharaVdeFaultProxy"
    ) as controller_class:
        controller = controller_class.return_value
        controller.insert.return_value = proxy = object()

        problem.inject_fault(params)

    problem.runtime.exec.assert_not_called()
    assert not hasattr(problem.runtime, "set_interface_state")
    controller.set_netem_loss.assert_called_once_with(proxy, 100)


def test_containerlab_link_flap_uses_host_veth_not_lab_node() -> None:
    problem = LinkFlap(None)
    problem.runtime = SimpleNamespace(backend="containerlab", exec=Mock())
    params = LinkFlapParams(host_name="leaf1", intf_name="e1-1", down_time=2, up_time=3)

    with patch(
        "nika.problems.link_interface.link.HostTcController"
    ) as controller_class:
        controller = controller_class.return_value
        controller.start_node_link_flap.return_value = "leaf1:e1-1"

        problem.inject_fault(params)

    problem.runtime.exec.assert_not_called()
    controller.start_node_link_flap.assert_called_once_with("leaf1", "e1-1", 2, 3)


def test_kathara_link_flap_uses_hidden_vde_proxy_not_lab_node() -> None:
    problem = LinkFlap(None)
    problem.runtime = SimpleNamespace(backend="kathara", exec=Mock())
    params = LinkFlapParams(
        host_name="router1", intf_name="eth0", down_time=2, up_time=3
    )

    with patch(
        "nika.problems.link_interface.link.KatharaVdeFaultProxy"
    ) as controller_class:
        controller = controller_class.return_value
        controller.insert.return_value = proxy = object()

        problem.inject_fault(params)

    problem.runtime.exec.assert_not_called()
    controller.start_link_flap.assert_called_once_with(proxy, 2, 3)
