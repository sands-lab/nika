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


def test_terse_rows_read_numbered_items_and_flag_only_routes():
    from nika.net_env.utils.routeros.parse import routeros_terse_rows

    output = (
        "Flags: D - dynamic; X - disabled\n"
        "0 D instance=NIKA interface=ether2 srcid=0000.0aff.0003 state=up\n"
        "Ab afi=ip contribution=active dst-address=10.0.10.0/24 "
        "routing-table=overlay gateway=172.30.0.9@overlay\n"
    )
    rows = routeros_terse_rows(output)
    assert [row.get("interface") for row in rows] == ["ether2", None]
    assert rows[1]["dst-address"] == "10.0.10.0/24"
    assert rows[1]["routing-table"] == "overlay"
    with pytest.raises(ValueError):
        routeros_terse_rows("syntax error (line 1 column 5)")
