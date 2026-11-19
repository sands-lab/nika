"""Flow query contracts and restrictions on collector execution."""

from __future__ import annotations

import json
import shlex

import pytest

from nika.service.kathara.netflow_api import NetflowAPIMixin, restricted_args

pytestmark = pytest.mark.unit


class Collector(NetflowAPIMixin):
    def __init__(self, output: str = "") -> None:
        self.output = output
        self.commands: list[str] = []

    def exec_cmd(self, host_name: str, command: str, timeout: float = 10) -> str:
        assert host_name == "telemetry_collector"
        self.commands.append(command)
        if "flow_inventory.json" in command:
            return json.dumps(
                {
                    "edge": {
                        "interfaces": {
                            "2": {"name": "eth0", "link_peers": ["host:eth0"]}
                        }
                    }
                }
            )
        if "cat /var/lib/flow_exporters" in command:
            return "172.28.0.3 edge\n"
        return self.output


def test_records_have_frozen_interface_context_tos_dscp_and_flags() -> None:
    api = Collector(
        "t0|t1|172.28.0.3|2|6|10.0.0.1|40000|10.0.0.2|80|185|....S.|1.5|4|80|20|6|120|1\n"
    )
    result = api.flow_records(exporter="edge")
    row = result["rows"][0]
    assert row["exporter"] == "edge"
    assert row["in_if_name"] == "eth0"
    assert row["link_peers"] == ["host:eth0"]
    assert (row["tos"], row["dscp"]) == (185, 46)
    assert row["tcp_flags"] == "....S."
    assert row["duration_seconds"] == 1.5
    assert "ip -o link" not in " ".join(api.commands)
    assert "-R /var/lib/flows" in result["command"]


def test_subnet_and_tos_aggregation_retains_exporter() -> None:
    api = Collector("t0|t1|172.28.0.3|10.0.0.0|184|2|3|480|20|6|120|1\n")
    result = api.flow_top_talkers(["srcip4/24", "tos"], limit=100000)
    assert result["rows"][0]["dscp"] == 46
    argv = shlex.split(result["command"])
    assert argv[argv.index("-A") + 1] == "router,srcip4/24,tos"
    assert argv[argv.index("-n") + 1] == "1000"


def test_filter_is_one_shell_quoted_argument() -> None:
    hostile = "proto udp' ; cat /etc/passwd ; echo '"
    result = Collector("No matching flows\n").flow_records(filter=hostile)
    assert f"({hostile})" in shlex.split(result["command"])
    assert result["rows"] == []


@pytest.mark.parametrize(
    "args",
    [
        ["-w", "/tmp/out"],
        ["-r", "/etc/passwd"],
        ["-R", "/var/lib"],
        ["-f", "/etc/passwd"],
        ["-C", "/etc/passwd"],
        ["-G", "/etc/passwd"],
        ["-M", "/tmp"],
        ["-z"],
        ["-x", "/var/lib/flows/a"],
        ["-E", "/tmp/a"],
        ["-o", "-w"],
        ["-n100"],
        ["--", "-r"],
        ["-o"],
    ],
)
def test_pass_through_rejects_file_operations_and_option_smuggling(args) -> None:
    with pytest.raises(ValueError):
        restricted_args(args)


def test_pass_through_is_bounded_and_preserves_query_options() -> None:
    result = Collector("a" * 100001).nfdump(
        ["-B", "-o", "fmt:%sa|%opkt", "-n", "99999", "proto tcp"]
    )
    assert len(result["text"]) == 100000
    assert result["truncated"]
    assert "timeout --verbose -s KILL 25s" in result["command"]
    assert "head -c 100001" in result["command"]
    assert result["exporters"] == {"edge": "172.28.0.3"}
    assert "-n 1000" in result["command"]


def test_conversations_request_reverse_counters() -> None:
    api = Collector()
    result = api.flow_conversations(exporter="edge")
    assert "-B" in result["command"]
    assert "%opkt" in result["command"] and "%obyt" in result["command"]
    with pytest.raises(ValueError, match="exporter"):
        api.flow_conversations()


def test_nfdump_errors_are_observation_errors() -> None:
    with pytest.raises(ValueError, match="Unknown protocol"):
        Collector("Line 1: Unknown protocol at 'bogus'\n").flow_records(
            filter="proto bogus"
        )


def test_pass_through_selects_flow_format_without_overriding_caller() -> None:
    args = ["-c", "3", "proto tcp"]
    argv = restricted_args(args)
    assert args == ["-c", "3", "proto tcp"]
    assert argv[argv.index("-o") + 1] == "line"
    assert argv[-1] == "proto tcp"
    assert argv[argv.index("-c") + 1] == "3"

    explicit = restricted_args(["-c", "3", "-o", "long"])
    assert explicit.count("-o") == 1
    assert explicit[explicit.index("-o") + 1] == "long"
    assert "-o" not in restricted_args(["-I"])
    bidirectional = restricted_args(["-B", "-c", "3"])
    fmt = bidirectional[bidirectional.index("-o") + 1]
    assert "%opkt" in fmt and "%obyt" in fmt
