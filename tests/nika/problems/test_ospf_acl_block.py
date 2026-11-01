"""SR Linux ospf_acl_block waits for Full adjacencies to drop."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nika.problems.forwarding_encapsulation_policy.acl import (
    OSPFAclBlock,
    OSPFAclBlockParams,
)

pytestmark = pytest.mark.unit


def _srl_ospf_acl_block(runtime: MagicMock) -> OSPFAclBlock:
    problem = OSPFAclBlock()
    problem.net_env = SimpleNamespace(machine_identities={})
    problem.runtime = runtime
    return problem


def test_srl_inject_waits_until_ospf_neighbors_are_down() -> None:
    runtime = MagicMock()
    runtime.backend = "containerlab"
    runtime.srl_add_ospf_acl_drop.return_value = [245]
    runtime.srl_ospf_neighbor_interfaces.return_value = []
    problem = _srl_ospf_acl_block(runtime)

    problem.inject_fault(OSPFAclBlockParams(host_name="berlin"))

    runtime.srl_add_ospf_acl_drop.assert_called_once_with("berlin")
    runtime.srl_ospf_neighbor_interfaces.assert_called_with("berlin")
    assert problem._srl_acl_entries == [245]


def test_srl_inject_raises_when_ospf_neighbors_stay_full(monkeypatch) -> None:
    runtime = MagicMock()
    runtime.backend = "containerlab"
    runtime.srl_add_ospf_acl_drop.return_value = [245]
    runtime.srl_ospf_neighbor_interfaces.return_value = ["ethernet-1/1.0"]
    problem = _srl_ospf_acl_block(runtime)
    monkeypatch.setattr(
        "nika.problems.forwarding_encapsulation_policy.acl.wait_until",
        lambda *_a, **_k: False,
    )

    with pytest.raises(RuntimeError, match="left Full adjacencies"):
        problem.inject_fault(OSPFAclBlockParams(host_name="berlin"))


def test_srl_verify_requires_acl_and_no_full_neighbors() -> None:
    runtime = MagicMock()
    runtime.backend = "containerlab"
    runtime.srl_ospf_acl_drop_present.return_value = True
    runtime.srl_ospf_neighbor_interfaces.return_value = []
    problem = _srl_ospf_acl_block(runtime)
    problem._srl_acl_entries = [245]
    params = OSPFAclBlockParams(host_name="berlin")

    assert problem.verify_fault(params)["verified"] is True

    runtime.srl_ospf_neighbor_interfaces.return_value = ["ethernet-1/1.0"]
    assert problem.verify_fault(params)["verified"] is False
