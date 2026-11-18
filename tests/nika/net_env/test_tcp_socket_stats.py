"""Stable socket identities and counters for TCP calibration and recovery."""

import pytest

from nika.net_env.verify import TcpSocketStats, _parse_tcp_socket_stats

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("prefix", ["", "  ", "ESTAB "])
def test_socket_stats_keep_endpoints_cookie_buffer_and_bytes(prefix):
    raw = (
        prefix + "0 42 10.1.10.2:40000 10.0.20.2:8080 ino:1 sk:abc "
        "skmem:(r0,rb8192,t0,tb16384) cubic bytes_received:65536\n"
        "0 0 [::ffff:10.1.10.2]:5201 [::ffff:10.0.20.2]:41000 "
        "ino:2 sk:def skmem:(r0,rb131072,t0,tb16384) cubic\n"
    )
    assert _parse_tcp_socket_stats(raw) == [
        TcpSocketStats("10.1.10.2:40000", "10.0.20.2:8080", "abc", 8192, 65536),
        TcpSocketStats(
            "[::ffff:10.1.10.2]:5201",
            "[::ffff:10.0.20.2]:41000",
            "def",
            131072,
            0,
        ),
    ]


@pytest.mark.parametrize("details", ["sk:abc", "skmem:(r0,rb8192)"])
def test_socket_stats_reject_incomplete_recovery_evidence(details):
    with pytest.raises(ValueError, match="identity or receive buffer missing"):
        _parse_tcp_socket_stats("0 0 10.1.10.2:40000 10.0.20.2:8080 " + details)


def test_empty_connection_inventory():
    assert _parse_tcp_socket_stats("") == []
