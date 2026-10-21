"""RouterOS startup-import readiness distinguishes booting from rejected configs."""

import pytest

from nika.net_env.utils.routeros.common import (
    routeros_config_applied,
    routeros_interface,
)

pytestmark = pytest.mark.unit

APPLIED = "Script file loaded and executed successfully"


def test_pending_imports_wait_and_rejected_imports_fail_fast():
    assert routeros_config_applied([("r1", APPLIED), ("r2", APPLIED + "\n")])
    for pending in ("", "[TIMEOUT] after 20s", "no such item (/file/get; line 1)"):
        assert not routeros_config_applied([("r1", APPLIED), ("r2", pending)])
    rejected = (
        "Script Error: failure: ptp params are invalid for passive circuit "
        "(/routing/isis/interface-template/add; line 11) (:import; line 1)"
    )
    with pytest.raises(RuntimeError, match="r2: Script Error"):
        routeros_config_applied([("r1", APPLIED), ("r2", rejected)])


def test_first_container_interface_is_reserved_for_management():
    assert routeros_interface("eth0") == "ether1"
    assert routeros_interface("eth1") == "ether2"
    assert routeros_interface("lo") == "lo0"
    with pytest.raises(ValueError):
        routeros_interface("wg0")
