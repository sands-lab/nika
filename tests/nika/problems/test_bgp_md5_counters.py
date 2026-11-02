"""Parse authentication counters and the peer paths used by the MD5 probe."""

import pytest

from nika.validation.symptom.bgp_md5_probes import tcp_md5_counters

pytestmark = pytest.mark.unit


def test_tcp_md5_counters_follow_kernel_field_names():
    assert tcp_md5_counters(
        "TcpExt: SyncookiesSent TCPMD5Failure TCPMD5NotFound TCPMD5Unexpected TCPAORequired\n"
        "TcpExt: 8 3 11 5 0\n"
        "IpExt: InNoRoutes\nIpExt: 17\n"
    ) == {"TCPMD5NotFound": 11, "TCPMD5Unexpected": 5, "TCPMD5Failure": 3}
    assert tcp_md5_counters("TcpExt: SyncookiesSent\nTcpExt: 8\n") == {}


def test_iosxr_neighbor_prefix_accepts_adjacent_internal_status(monkeypatch):
    from nika.net_env.utils.iosxr.fault_ops import IosxrFaultOps

    ops = IosxrFaultOps(None, "router")
    monkeypatch.setattr(
        ops,
        "_show",
        lambda _command: (
            "   Network            Next Hop            Metric LocPrf Weight Path\n"
            "*>i203.0.114.0/24     10.255.0.11              0    100      0 i\n"
            "*> 198.51.100.0/24    10.0.0.4                 0             0 65001 i\n"
        ),
    )
    assert ops.bgp_received_prefixes("10.255.0.11") == {
        "203.0.114.0/24",
        "198.51.100.0/24",
    }
