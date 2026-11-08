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
from nika.mcp.registry import (
    DIAGNOSIS_PACKET_CAPTURE_SERVER,
    MCP_SERVER_SPECS,
    select_diagnosis_servers,
)
from nika.service.packet_capture import inspect
from nika.service.packet_capture.artifact import read_meta, remote_meta_path
from nika.service.packet_capture.limits import (
    HARD_INSPECT_PAGE_SIZE,
    HARD_MAX_DURATION_SEC,
    HARD_MAX_PACKETS,
    clamp_inspect_limit,
    clamp_start_limits,
)
from nika.service.packet_capture.manager import CaptureManager
from nika.service.packet_capture.protocol_fields import extract_protocol_fields
from nika.service.containerlab.srl_api import srl_linux_intf
from tests.support.integration_base import SharedSessionTestCase
from tests.support.prerequisites import docker_available


# --- limits -----------------------------------------------------------------


@pytest.mark.unit
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


@pytest.mark.unit
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


@pytest.mark.unit
class TestProtocolFields:
    @pytest.mark.parametrize(
        "protocol,layer,expected",
        [
            (
                "dns",
                {
                    "Queries": {
                        "example.local: type A": {"dns.qry.name": "example.local"}
                    },
                    "dns.flags_tree": {"dns.flags.rcode": "3"},
                },
                {"qname": "example.local", "rcode": "3"},
            ),
            (
                "bgp",
                {
                    "BGP OPEN": {
                        "bgp.type": "1",
                        "bgp.open.myas": "23456",
                        "bgp.open.holdtime": "180",
                        "bgp.open.identifier": "10.0.0.1",
                        "Capabilities": {
                            "bgp.cap.4as": "4200000001",
                            "bgp.cap.type": "65",
                        },
                    }
                },
                {
                    "type": "1",
                    "asn": "4200000001",
                    "hold_time": "180",
                    "router_id": "10.0.0.1",
                    "capabilities": "65",
                },
            ),
            (
                "ospf",
                {
                    "Header": {
                        "ospf.msg": "1",
                        "ospf.area_id": "0.0.0.0",
                        "ospf.srcrouter": "10.0.0.1",
                    },
                    "Hello": {
                        "ospf.hello.hello_interval": "10",
                        "ospf.hello.router_dead_interval": "40",
                        "ospf.hello.active_neighbor": "10.0.0.2",
                    },
                },
                {
                    "msg_type": "1",
                    "area": "0.0.0.0",
                    "router_id": "10.0.0.1",
                    "hello_interval": "10",
                    "dead_interval": "40",
                    "neighbor": "10.0.0.2",
                },
            ),
            (
                "tls",
                [
                    {
                        "Record Layer": {
                            "tls.handshake": {
                                "tls.handshake.extensions_server_name": "example.local"
                            }
                        }
                    }
                ],
                {"sni": "example.local"},
            ),
        ],
    )
    def test_nested_tshark_protocol_trees(self, protocol, layer, expected) -> None:
        fields = extract_protocol_fields(protocol, _packet({protocol: layer}))
        for key, value in expected.items():
            assert fields[key] == value

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
                        "bgp.open.myas": ["65001"],
                        "bgp.open.holdtime": ["180"],
                        "bgp.open.identifier": ["10.0.0.1"],
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


@pytest.mark.unit
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
                "/tmp/capture-abc.pcapng",
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
                "/tmp/capture-abc.pcapng",
                view="protocol",
            )

    def test_missing_tshark_raises(self) -> None:
        runtime = MagicMock()
        runtime.exec.return_value = ""
        with pytest.raises(inspect.TsharkNotFoundError):
            inspect.require_tshark(runtime, "pc1")

    def test_node_without_tshark_inspects_host_side(self) -> None:
        runtime = MagicMock()
        runtime.exec.return_value = ""
        with (
            patch.object(inspect, "_read_pcap", return_value=b"PCAP") as read,
            patch.object(inspect, "_host_tshark", return_value="1\n") as host,
        ):
            out = inspect._run_tshark(
                runtime, "s1", "/tmp/x.pcapng", ["-r", "/tmp/x.pcapng", "-Y", "ip"]
            )
        assert out == "1\n"
        read.assert_called_once_with(runtime, "s1", "/tmp/x.pcapng")
        assert host.call_args.args[0] == ["-r", "-", "-Y", "ip"]
        assert host.call_args.kwargs["stdin"] == b"PCAP"

    def test_node_with_tshark_runs_in_node(self) -> None:
        runtime = MagicMock()
        runtime.exec.return_value = "/usr/bin/tshark"
        with patch.object(inspect, "_host_tshark") as host:
            inspect._run_tshark(runtime, "pc1", "/tmp/x", ["-r", "/tmp/x"])
        host.assert_not_called()
        assert "tshark -r /tmp/x" in runtime.exec.call_args.args[1]


# --- registry / gateway -----------------------------------------------------


@pytest.mark.unit
class TestPacketCaptureRegistry:
    def test_selected_by_default(self) -> None:
        servers = select_diagnosis_servers("simple_bgp", backend="kathara")
        assert DIAGNOSIS_PACKET_CAPTURE_SERVER in servers
        assert servers.index(DIAGNOSIS_PACKET_CAPTURE_SERVER) == 2
        assert "kathara_frr_mcp_server" not in servers

    def test_gateway_mount_registered(self) -> None:
        assert DIAGNOSIS_PACKET_CAPTURE_SERVER in MCP_SERVER_SPECS

    def test_start_targets_device(self) -> None:
        assert TOOL_NODE_ARGUMENTS["packet_capture_start"] == ("device",)


# --- mocked lifecycle -------------------------------------------------------


class FakeRuntime:
    def __init__(self) -> None:
        self.commands: list[tuple[str, str]] = []
        self.files: dict[tuple[str, str], str] = {}
        self.nodes: list[str] = ["client1", "client2", "router1"]

    def list_nodes(self) -> list[str]:
        return list(self.nodes)

    def write_file(self, node: str, path: str, content: str) -> str:
        self.files[(node, path)] = content
        return ""

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
        if command.startswith("cat /tmp/capture-") and command.endswith(".pid"):
            return "4242"
        if command.startswith("cat /tmp/capture-") and ".meta.json" in command:
            path = command[len("cat ") :].strip()
            return self.files.get((node, path), "")
        if "test -f" in command and "echo yes" in command:
            # test -f <path> && echo yes || echo no
            parts = command.split()
            path = parts[2] if len(parts) > 2 else ""
            return (
                "yes"
                if (node, path) in self.files or path.endswith(".pcapng")
                else "no"
            )
        if "wc -c <" in command:
            return "11"
        if "tcpdump -r" in command and "wc -l" in command:
            return "2"
        if "dumpcap -v" in command:
            return ""
        if command == "sleep 0.5":
            return ""
        return ""


@pytest.mark.unit
class TestCaptureLifecycle:
    def test_zero_packets_still_has_a_valid_capture_file(self) -> None:
        class EmptyCaptureRuntime(FakeRuntime):
            def exec(self, node, command, timeout=10):
                if "wc -c <" in command:
                    return "24"
                if "tcpdump -r" in command and "wc -l" in command:
                    return "0"
                return super().exec(node, command, timeout)

        manager = CaptureManager(runtime=EmptyCaptureRuntime())
        capture_id = manager.start(device="client1", interface="eth0")["capture_id"]
        stopped = manager.stop(capture_id)
        assert stopped["packet_count"] == 0
        assert stopped["captured_bytes"] == 24
        assert stopped["artifact"]["remote_path"]

    def test_missing_output_is_failed_not_an_empty_artifact(self) -> None:
        class MissingOutputRuntime(FakeRuntime):
            def exec(self, node, command, timeout=10):
                if "wc -c <" in command:
                    return "missing"
                return super().exec(node, command, timeout)

        runtime = MissingOutputRuntime()
        manager = CaptureManager(runtime=runtime)
        capture_id = manager.start(device="client1", interface="eth0")["capture_id"]
        with pytest.raises(FileNotFoundError, match="no file"):
            manager.stop(capture_id)
        assert read_meta(runtime, capture_id)["status"] == "failed"
        with pytest.raises(RuntimeError, match="failed"):
            manager.stop(capture_id)
        with pytest.raises(RuntimeError, match="failed"):
            manager.inspect(capture_id, view="summary")

    def test_concurrent_captures_have_distinct_ids(self) -> None:
        runtime = FakeRuntime()
        manager = CaptureManager(runtime=runtime)

        first = manager.start(device="client1", interface="eth0")
        second = manager.start(
            device="client2", interface="eth1", capture_filter="icmp"
        )

        assert first["capture_id"] != second["capture_id"]
        assert first["status"] == "running"
        assert read_meta(runtime, first["capture_id"])["device"] == "client1"

    def test_stop_persists_meta_on_node_only(self, tmp_path: Path) -> None:
        runtime = FakeRuntime()
        manager = CaptureManager(runtime=runtime)
        started = manager.start(device="router1", interface="eth0")
        capture_id = started["capture_id"]

        stopped = manager.stop(capture_id)

        assert stopped["packet_count"] == 2
        assert stopped["captured_bytes"] == 11
        assert stopped["artifact"]["device"] == "router1"
        assert stopped["artifact"]["remote_path"] == f"/tmp/capture-{capture_id}.pcapng"
        assert stopped["artifact"]["tshark_version"] == "TShark 4.0.0"
        meta = read_meta(runtime, capture_id)
        assert meta["status"] == "stopped"
        assert meta["remote_path"] == f"/tmp/capture-{capture_id}.pcapng"
        assert (
            "router1",
            remote_meta_path(capture_id),
        ) in runtime.files
        assert not (tmp_path / "packet_captures").exists()

    def test_inspect_requires_stopped_capture(self) -> None:
        runtime = FakeRuntime()
        manager = CaptureManager(runtime=runtime)
        started = manager.start(device="client1", interface="eth0")
        with pytest.raises(RuntimeError, match="not stopped"):
            manager.inspect(started["capture_id"], view="summary")

    def test_missing_interface_fails_before_capture(self) -> None:
        class _NoSuchIface(FakeRuntime):
            def exec(self, node: str, command: str, timeout: float = 10) -> str:
                if command.startswith("[ -e /sys/class/net/"):
                    self.commands.append((node, command))
                    return "missing"
                if command.startswith("ls /sys/class/net"):
                    return "lo\neth0\n"
                return super().exec(node, command, timeout)

        runtime = _NoSuchIface()
        with pytest.raises(ValueError, match=r"'eth9'.*Available interfaces: lo, eth0"):
            CaptureManager(runtime=runtime).start(device="client1", interface="eth9")
        assert not any("nohup" in command for _node, command in runtime.commands)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "device"),
    [
        ("ethernet-1/3", "e1-3"),
        ("ethernet-1/3.0", "e1-3"),
        ("e1-3", "e1-3"),
        ("e1-3.0", "e1-3"),
        ("eth2", "e1-3"),
        ("mgmt0", "mgmt0"),
        ("any", "any"),
    ],
)
def test_srl_names_map_to_default_netns_port(name: str, device: str) -> None:
    assert srl_linux_intf(name) == device


@pytest.mark.unit
@pytest.mark.parametrize(
    ("sonic", "name", "device"),
    [
        (True, "Ethernet0", "eth1"),
        (True, "Ethernet8", "eth3"),
        (True, "eth2", "eth2"),
        (False, "Ethernet0", "Ethernet0"),
    ],
)
def test_sonic_ports_map_to_container_data_interface(
    sonic: bool, name: str, device: str
) -> None:
    from nika.service.packet_capture.capture_backend import capture_device

    runtime = MagicMock(backend="containerlab")
    runtime.uses_srl_router.return_value = False
    runtime.exec.side_effect = lambda node, command, timeout=None: (
        "sonic" if "sonic-cfggen" in command and sonic else ""
    )
    assert capture_device(runtime, "leaf1", name) == device


# --- live e2e ---------------------------------------------------------------


@pytest.mark.e2e
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
        return CaptureManager(runtime=runtime)

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
        assert stopped["artifact"]["remote_path"] == f"/tmp/capture-{capture_id}.pcapng"

        meta = read_meta(runtime, capture_id)
        assert meta["status"] == "stopped"
        assert meta["device"] == self.CAPTURE_HOST
        assert meta["remote_path"] == f"/tmp/capture-{capture_id}.pcapng"
        assert "ground_truth" not in json.dumps(meta)
        assert not (session_dir / "packet_captures").exists()
        remote_meta = runtime.exec(
            self.CAPTURE_HOST,
            f"test -f /tmp/capture-{capture_id}.meta.json && echo yes",
            timeout=5,
        ).strip()
        assert remote_meta == "yes"

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

        assert not (session_dir / "packet_captures").exists()

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
        assert stop_a["artifact"]["remote_path"].startswith("/tmp/capture-")
        assert stop_b["artifact"]["remote_path"].startswith("/tmp/capture-")

    def test_all_inspect_views_and_container_pcap_persistence(self) -> None:
        """Verify pcap stays on the node and every inspect view works in Docker."""
        manager = self._manager()
        runtime = self._runtime()
        session_dir = Path(self._session_row(self.session_id)["session_dir"])

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
        assert not (session_dir / "packet_captures").exists()

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
        assert not (session_dir / "packet_captures").exists()
