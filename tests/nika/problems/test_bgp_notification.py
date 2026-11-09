"""Decode received Cease notifications without confusing their direction or data."""

import pytest

from nika.net_env.utils.routeros.parse import routeros_bgp_received_notification

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("data", [b"", b"\x00\x01\x01\x00\x00\x00\x0a"])
def test_received_prefix_cease_allows_optional_afi_safi_limit(data):
    # RFC 4486: marker, length, NOTIFICATION, Cease/Maximum Prefixes, data.
    wire = b"\xff" * 16 + (21 + len(data)).to_bytes(2, "big") + b"\x03\x06\x01" + data
    text = f"output.procid=4 input.procid=5 .last-notification={wire.hex()}"
    assert (
        routeros_bgp_received_notification(text)
        == "Cease/Maximum Number of Prefixes Reached"
    )
    assert (
        routeros_bgp_received_notification(
            f"output.procid=4 .last-notification={wire.hex()} input.procid=5"
        )
        is None
    )
    assert (
        routeros_bgp_received_notification(
            f"input.procid=5 output.procid=4 .last-notification={wire.hex()}"
        )
        is None
    )
