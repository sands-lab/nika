"""Native BGP hijack inject fails when the origination never appears."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nika.problems.security.bgp import BGPHijacking, BGPHijackingParams

pytestmark = pytest.mark.unit


def test_native_inject_fails_when_prefix_is_never_advertised(monkeypatch) -> None:
    ops = MagicMock()
    ops.profile = "sonic"
    ops.save_config.return_value = "baseline"
    ops.bgp_advertised_to.return_value = []
    monkeypatch.setattr(
        "nika.problems.security.bgp.native_fault_ops", lambda *a, **k: ops
    )
    monkeypatch.setattr("nika.problems.security.bgp.wait_until", lambda *a, **k: False)
    problem = BGPHijacking(None)
    problem.runtime = SimpleNamespace(backend="containerlab")
    problem.net_env = SimpleNamespace(
        backend="containerlab",
        machine_identities={"stuttgart": SimpleNamespace(capabilities=["sonic"])},
    )
    params = BGPHijackingParams(host_name="stuttgart", target_network="203.0.113.0/24")
    with pytest.raises(RuntimeError, match="did not advertise hijacked prefix"):
        problem.inject_fault(params)
    ops.originate_bgp_prefix.assert_called_once_with("203.0.113.0/24")
    monkeypatch.setattr(
        "nika.problems.security.bgp.native_fault_ops",
        lambda *a, **k: pytest.fail("Recovery lost the injecting provider's state"),
    )
    assert problem.recover_fault(params)["verified"]
    ops.restore_config.assert_called_once_with("baseline")
