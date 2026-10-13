from __future__ import annotations

import pytest
from nika.service.pingmesh.parser import parse_ping_output

pytestmark = pytest.mark.unit

OK_OUTPUT = "\nPING 10.0.0.2 (10.0.0.2) 56(84) bytes of data.\n\n--- 10.0.0.2 ping statistics ---\n4 packets transmitted, 4 received, 0% packet loss, time 3005ms\nrtt min/avg/max/mdev = 0.045/0.062/0.089/0.018 ms\n"
LOSS_OUTPUT = "\n--- 10.0.0.2 ping statistics ---\n4 packets transmitted, 2 received, 50% packet loss, time 3005ms\nrtt min/avg/max/mdev = 0.045/0.062/0.089/0.018 ms\n"
DOWN_OUTPUT = "\n--- 10.0.0.2 ping statistics ---\n4 packets transmitted, 0 received, +1 errors, 100% packet loss, time 3050ms\n"
NO_RTT_OUTPUT = "\n--- 10.0.0.2 ping statistics ---\n4 packets transmitted, 4 received, 0% packet loss, time 3005ms\n"


class ParsePingOutputTest:
    def test_ok_output(self) -> None:
        stats = parse_ping_output(OK_OUTPUT)
        assert stats["tx"] == 4
        assert stats["rx"] == 4
        assert stats["loss_percent"] == 0.0
        assert stats["status"] == "ok"
        assert stats["rtt_avg_ms"] == pytest.approx(0.062)

    def test_partial_loss(self) -> None:
        stats = parse_ping_output(LOSS_OUTPUT)
        assert stats["status"] == "ok"
        assert stats["loss_percent"] == 50.0

    def test_unreachable(self) -> None:
        stats = parse_ping_output(DOWN_OUTPUT)
        assert stats["status"] == "down"
        assert stats["loss_percent"] == 100.0
        assert stats["rx"] == 0

    def test_missing_rtt(self) -> None:
        stats = parse_ping_output(NO_RTT_OUTPUT)
        assert stats["status"] == "ok"
        assert stats["rtt_avg_ms"] is None

    def test_network_unreachable(self) -> None:
        stats = parse_ping_output("ping: connect: Network is unreachable")
        assert stats["status"] == "down"
        assert stats["loss_percent"] == 100.0
        stats = parse_ping_output("command not found")
        assert stats["status"] == "unknown"
        assert stats["tx"] is None
