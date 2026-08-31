"""Packet capture unit, contract, lifecycle, and live e2e tests."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from nika.runtime.factory import runtime_for_session
from nika.mcp.gateway.access import TOOL_NODE_ARGUMENTS
from nika.mcp.gateway.app import _MCP_MODULE_ATTRS
from nika.mcp.registry import (
    DIAGNOSIS_PACKET_CAPTURE_SERVER,
    select_diagnosis_servers,
)
from nika.service.packet_capture import inspect
from nika.service.packet_capture.artifact import meta_path, read_meta
from nika.service.packet_capture.limits import (
    HARD_INSPECT_PAGE_SIZE,
    HARD_MAX_DURATION_SEC,
    HARD_MAX_PACKETS,
    clamp_inspect_limit,
    clamp_start_limits,
)
from nika.service.packet_capture.manager import CaptureManager
from nika.service.packet_capture.protocol_fields import extract_protocol_fields
from tests.support.integration_base import SharedSessionTestCase
from tests.support.prerequisites import docker_available


# --- limits -----------------------------------------------------------------


class TestClampStartLimits:
    def test_uses_defaults_when_agent_omits_args(self) -> None:
        limits = clamp_start_limits(
            max_duration_sec=None,
            max_packets=None,
            max_bytes=None,
        )
        assert limits.max_duration_sec == HARD_MAX_DURATION_SEC
        assert limits.max_packets == HARD_MAX_PACKETS

    def test_accepts_agent_values_within_ceiling(self) -> None:
        limits = clamp_start_limits(
            max_duration_sec=10,
            max_packets=100,
            max_bytes=None,
        )
        assert limits.max_duration_sec == 10
        assert limits.max_packets == 100

    def test_clamps_agent_values_above_ceiling(self) -> None:
        limits = clamp_start_limits(
            max_duration_sec=60,
            max_packets=5000,
            max_bytes=None,
        )
        assert limits.max_duration_sec == HARD_MAX_DURATION_SEC
        assert limits.max_packets == HARD_MAX_PACKETS

    def test_rejects_non_positive_duration(self) -> None:
        with pytest.raises(ValueError, match="max_duration_sec"):
            clamp_start_limits(max_duration_sec=0, max_packets=None, max_bytes=None)


class TestClampInspectLimit:
    def test_defaults_to_page_size(self) -> None:
        assert clamp_inspect_limit(None) == HARD_INSPECT_PAGE_SIZE

    def test_clamps_above_ceiling(self) -> None:
        assert clamp_inspect_limit(100) == HARD_INSPECT_PAGE_SIZE

    def test_accepts_agent_page_size(self) -> None:
        assert clamp_inspect_limit(10) == 10


# --- protocol fields --------------------------------------------------------


def _packet(layers: dict) -> dict:
    return {"_source": {"layers": layers}}


class TestProtocolFields:
    def test_tcp_fields(self) -> None:
        fields = extract_protocol_fields(
            "tcp",
            _packet(
                {
                    "frame": {"frame.number": ["1"], "frame.time": ["0.0"]},
                    "tcp": {
                        "tcp.srcport": ["1234"],
                        "tcp.dstport": ["80"],
                        "tcp.flags": ["0x018"],
                        "tcp.seq": ["1"],
                        "tcp.ack": ["2"],
                        "tcp.window_size_value": ["8192"],
                        "tcp.analysis.flags": ["1"],
                    },
                }
            ),
        )
        assert fields["src_port"] == "1234"
        assert fields["dst_port"] == "80"
        assert fields["flags"] == "0x018"

    def test_dns_fields(self) -> None:
        fields = extract_protocol_fields(
            "dns",
            _packet(
                {
                    "frame": {"frame.number": ["2"]},
                    "dns": {
                        "dns.qry.name": ["example.local"],
                        "dns.qry.type": ["1"],
                        "dns.flags.rcode": ["3"],
                        "dns.count.answers": ["0"],
                        "dns.flags.response": ["1"],
                    },
                }
            ),
        )
        assert fields["qname"] == "example.local"
        assert fields["rcode"] == "3"

    def test_bgp_fields(self) -> None:
        fields = extract_protocol_fields(
            "bgp",
            _packet(
                {
                    "frame": {"frame.number": ["3"]},
                    "bgp": {
                        "bgp.type": ["1"],
                        "bgp.as": ["65001"],
                        "bgp.hold_time": ["180"],
                        "bgp.identifier": ["10.0.0.1"],
                    },
                }
            ),
        )
        assert fields["asn"] == "65001"
        assert fields["router_id"] == "10.0.0.1"

    def test_unknown_protocol_lists_layers(self) -> None:
        fields = extract_protocol_fields(
            "foo",
            _packet({"frame": {"frame.number": ["4"]}, "arp": {}, "ip": {}}),
        )
        assert "arp" in fields["layers"]
        assert "ip" in fields["layers"]


# --- inspect ----------------------------------------------------------------


class TestInspectCapture:
    def test_packets_view_reports_truncation(self) -> None:
        runtime = MagicMock()
        with patch.object(inspect, "_run_tshark") as mock_tshark:
            mock_tshark.side_effect = [
                "1\n",
                "1|0.0|14|Ethernet|||||",
            ]
            payload = inspect.inspect_capture(
                runtime,
                "pc1",
                "/tmp/nika-capture-abc.pcapng",
                view="packets",
                limit=1,
                offset=0,
            )
        assert payload["view"] == "packets"
        assert payload["returned"] == 1
        assert payload["total_available"] == 1
        assert payload["truncated"] is False
        assert payload["data"]["packets"][0]["protocol"] == "Ethernet"

    def test_protocol_view_requires_protocol(self) -> None:
        runtime = MagicMock()
        with pytest.raises(ValueError, match="protocol is required"):
            inspect.inspect_capture(
                runtime,
                "pc1",
                "/tmp/nika-capture-abc.pcapng",
                view="protocol",
            )

    def test_missing_tshark_raises(self) -> None:
        runtime = MagicMock()
        runtime.exec.return_value = ""
        with pytest.raises(inspect.TsharkNotFoundError):
            inspect.require_tshark(runtime, "pc1")


# --- registry / gateway -----------------------------------------------------


class TestPacketCaptureRegistry:
    def test_selected_by_default(self) -> None:
        servers = select_diagnosis_servers("simple_bgp", backend="kathara")
        assert DIAGNOSIS_PACKET_CAPTURE_SERVER in servers
        assert servers.index(DIAGNOSIS_PACKET_CAPTURE_SERVER) < servers.index(
            "kathara_frr_mcp_server"
        )

    def test_gateway_mount_registered(self) -> None:
        assert DIAGNOSIS_PACKET_CAPTURE_SERVER in _MCP_MODULE_ATTRS

    def test_start_targets_device(self) -> None:
        assert TOOL_NODE_ARGUMENTS["packet_capture_start"] == ("device",)


# --- mocked lifecycle -------------------------------------------------------


class FakeRuntime:
    def __init__(self) -> None:
        self.commands: list[tuple[str, str]] = []

    def exec(self, node: str, command: str, timeout: float = 10) -> str:
        self.commands.append((node, command))
        if "command -v dumpcap" in command:
            return ""
        if "command -v tcpdump" in command:
            return "/usr/bin/tcpdump"
        if "command -v tshark" in command:
            return "/usr/bin/tshark"
        if "tshark -v" in command:
            return "TShark 4.0.0"
        if "echo $! >" in command and "nohup" in command:
            return ""
        if command.startswith("cat /tmp/nika-capture-"):
            return "4242"
        if "wc -c <" in command:
            return "11"
        if "tcpdump -r" in command and "wc -l" in command:
            return "2"
        if "dumpcap -v" in command:
            return ""
        if "test -f" in command and "echo yes" in command:
            return "yes"
        if command == "sleep 0.5":
            return ""
        return ""


class TestCaptureLifecycle:
    def test_concurrent_captures_have_distinct_ids(self, tmp_path: Path) -> None:
        runtime = FakeRuntime()
        manager = CaptureManager(session_dir=str(tmp_path), runtime=runtime)

        first = manager.start(device="client1", interface="eth0")
        second = manager.start(
            device="client2", interface="eth1", capture_filter="icmp"
        )

        assert first["capture_id"] != second["capture_id"]
        assert first["status"] == "running"
        assert read_meta(str(tmp_path), first["capture_id"])["device"] == "client1"

    def test_stop_persists_meta_without_local_pcap(self, tmp_path: Path) -> None:
        runtime = FakeRuntime()
        manager = CaptureManager(session_dir=str(tmp_path), runtime=runtime)
        started = manager.start(device="router1", interface="eth0")
        capture_id = started["capture_id"]

        stopped = manager.stop(capture_id)

        assert stopped["packet_count"] == 2
        assert stopped["captured_bytes"] == 11
        assert stopped["artifact"]["device"] == "router1"
        assert (
            stopped["artifact"]["remote_path"]
            == f"/tmp/nika-capture-{capture_id}.pcapng"
        )
        assert stopped["artifact"]["tshark_version"] == "TShark 4.0.0"
        meta = json.loads(
            (tmp_path / "packet_captures" / capture_id / "meta.json").read_text()
        )
        assert meta["status"] == "stopped"
        assert meta["remote_path"] == f"/tmp/nika-capture-{capture_id}.pcapng"
        assert (
            not meta_path(str(tmp_path), capture_id)
            .parent.joinpath("capture.pcapng")
            .exists()
        )

    def test_inspect_requires_stopped_capture(self, tmp_path: Path) -> None:
        runtime = FakeRuntime()
        manager = CaptureManager(session_dir=str(tmp_path), runtime=runtime)
        started = manager.start(device="client1", interface="eth0")
        with pytest.raises(RuntimeError, match="not stopped"):
            manager.inspect(started["capture_id"], view="summary")


# --- live e2e ---------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class PacketCaptureLiveE2ETest(SharedSessionTestCase):
    """Exercise CaptureManager against a real Kathara simple_bgp lab."""

    SCENARIO = "simple_bgp"
    ENV_RUN_ARGS: ClassVar[list[str]] = []
    CAPTURE_HOST = "pc1"
    CAPTURE_IFACE = "eth0"
    PING_TARGET = "195.11.14.1"

    def _manager(self) -> CaptureManager:
        row = self._session_row(self.session_id)
        runtime = runtime_for_session(row)
        return CaptureManager(session_dir=str(row["session_dir"]), runtime=runtime)

    def _runtime(self):
        return runtime_for_session(self._session_row(self.session_id))

    def test_start_ping_stop_inspect_workflow(self) -> None:
        manager = self._manager()
        session_dir = Path(self._session_row(self.session_id)["session_dir"])
        runtime = self._runtime()

        assert self.CAPTURE_HOST in runtime.list_nodes()
        tools = runtime.exec(
            self.CAPTURE_HOST,
            "command -v tcpdump; command -v dumpcap || true; command -v tshark || true",
            timeout=10,
        )
        assert "tcpdump" in tools or "dumpcap" in tools, tools
        assert "tshark" in tools, tools

        started = manager.start(
            device=self.CAPTURE_HOST,
            interface=self.CAPTURE_IFACE,
            capture_filter="icmp",
            max_duration_sec=20,
            max_packets=200,
        )
        capture_id = started["capture_id"]
        assert started["status"] == "running"

        time.sleep(1.0)
        ping_out = runtime.exec(
            self.CAPTURE_HOST,
            f"ping -c 4 -W 2 {self.PING_TARGET}",
            timeout=20,
        )
        assert "bytes from" in ping_out or "icmp_seq" in ping_out.lower(), ping_out

        stopped = manager.stop(capture_id)
        assert stopped["packet_count"] >= 1, stopped
        assert stopped["captured_bytes"] > 0, stopped
        assert stopped["artifact"]["device"] == self.CAPTURE_HOST
        assert (
            stopped["artifact"]["remote_path"]
            == f"/tmp/nika-capture-{capture_id}.pcapng"
        )

        meta = read_meta(str(session_dir), capture_id)
        assert meta["status"] == "stopped"
        assert meta["device"] == self.CAPTURE_HOST
        assert meta["remote_path"] == f"/tmp/nika-capture-{capture_id}.pcapng"
        assert "ground_truth" not in json.dumps(meta)
        assert not (
            session_dir / "packet_captures" / capture_id / "capture.pcapng"
        ).exists()

        inspected = manager.inspect(
            capture_id,
            view="packets",
            display_filter="icmp",
            limit=10,
            offset=0,
        )
        assert inspected["returned"] >= 1
        packets = inspected["data"]["packets"]
        joined = " ".join(
            f"{row.get('protocol', '')} {row.get('info', '')}" for row in packets
        ).lower()
        assert "icmp" in joined, inspected

        summary = manager.inspect(capture_id, view="summary")
        assert "protocols" in summary["data"]

    def test_concurrent_captures_on_two_hosts(self) -> None:
        manager = self._manager()
        runtime = self._runtime()

        first = manager.start(
            device="pc1",
            interface="eth0",
            capture_filter="icmp",
            max_duration_sec=15,
            max_packets=100,
        )
        second = manager.start(
            device="pc2",
            interface="eth0",
            capture_filter="icmp",
            max_duration_sec=15,
            max_packets=100,
        )
        assert first["capture_id"] != second["capture_id"]

        time.sleep(0.8)
        runtime.exec("pc1", f"ping -c 2 -W 2 {self.PING_TARGET}", timeout=15)
        runtime.exec("pc2", "ping -c 2 -W 2 200.1.1.1", timeout=15)

        stop_a = manager.stop(first["capture_id"])
        stop_b = manager.stop(second["capture_id"])
        assert stop_a["packet_count"] >= 1, stop_a
        assert stop_b["packet_count"] >= 1, stop_b
        assert stop_a["artifact"]["remote_path"].startswith("/tmp/nika-capture-")
        assert stop_b["artifact"]["remote_path"].startswith("/tmp/nika-capture-")

    def test_all_inspect_views_and_container_pcap_persistence(self) -> None:
        """Verify pcap stays on the node and every inspect view works in Docker."""
        manager = self._manager()
        runtime = self._runtime()

        started = manager.start(
            device=self.CAPTURE_HOST,
            interface=self.CAPTURE_IFACE,
            capture_filter="icmp",
            max_duration_sec=15,
            max_packets=100,
        )
        capture_id = started["capture_id"]

        time.sleep(0.5)
        runtime.exec(
            self.CAPTURE_HOST,
            f"ping -c 3 -W 2 {self.PING_TARGET}",
            timeout=15,
        )

        stopped = manager.stop(capture_id)
        remote_path = stopped["artifact"]["remote_path"]
        assert stopped["packet_count"] >= 1, stopped

        exists = runtime.exec(
            self.CAPTURE_HOST,
            f"test -f {remote_path} && wc -c < {remote_path}",
            timeout=10,
        )
        size = int(exists.strip().splitlines()[-1])
        assert size > 0, exists
        assert size == stopped["captured_bytes"], (size, stopped)

        protocol = manager.inspect(
            capture_id,
            view="protocol",
            protocol="icmp",
            limit=10,
        )
        assert protocol["returned"] >= 1, protocol
        assert protocol["data"]["protocol"] == "icmp"

        expert = manager.inspect(capture_id, view="expert")
        assert "items" in expert["data"]

        missing = manager.inspect(
            capture_id,
            view="packets",
            display_filter="icmp and frame.number > 99999",
            limit=10,
        )
        assert missing["returned"] == 0
        assert missing["total_available"] == 0
