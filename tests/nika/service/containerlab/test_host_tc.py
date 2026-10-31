"""Parse the far host end of a Containerlab wire from ``tc filter show``."""

from __future__ import annotations

import pytest

from nika.service.containerlab.host_tc import HostTcController

pytestmark = pytest.mark.unit

# iproute2 6.1 ``tc filter show dev <peer> ingress`` after
# ``tc filter replace ... matchall action mirred egress redirect dev <far>``.
_MIRRED_INGRESS = """\
filter protocol all pref 1 matchall chain 0
filter protocol all pref 1 matchall chain 0 handle 0x1
  action order 1: mirred (Egress Redirect to device nw12ab34567890) stolen
	index 1 ref 1 bind 1
"""


def test_wire_partner_reads_mirred_redirect_device() -> None:
    controller = HostTcController(object())
    controller._run = lambda *args: _MIRRED_INGRESS
    assert controller.wire_partner("nw111111111111") == "nw12ab34567890"


def test_wire_partner_without_redirect_is_absent() -> None:
    controller = HostTcController(object())
    controller._run = lambda *args: "filter protocol all pref 1 matchall chain 0\n"
    assert controller.wire_partner("nw111111111111") is None
