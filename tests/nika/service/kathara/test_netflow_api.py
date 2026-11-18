"""Contracts for the IPFIX ``netflow_query`` service API."""

from __future__ import annotations

import shlex

import pytest

from nika.service.kathara.netflow_api import NetflowAPIMixin

pytestmark = pytest.mark.unit

_EXPORTERS = "172.28.0.3 chinng\n172.28.0.6 iplsng\n"


class _FakeCollector(NetflowAPIMixin):
    def __init__(self, nfdump_output: str) -> None:
        self.nfdump_output = nfdump_output
        self.commands: list[str] = []

    def exec_cmd(self, host_name: str, command: str, timeout: float = 10) -> str:
        assert host_name == "telemetry_collector"
        self.commands.append(command)
        if command.startswith("cat "):
            return _EXPORTERS
        return self.nfdump_output


def _nfdump_argv(api: _FakeCollector) -> list[str]:
    command = api.commands[-1]
    assert command.startswith("TZ=UTC ")
    return shlex.split(command.removeprefix("TZ=UTC "))


def test_records_are_parsed_and_exporter_ip_is_named() -> None:
    api = _FakeCollector(
        "2026-10-07 15:56:13.433|2026-10-07 15:56:28.001|  172.28.0.3|     2|17   |"
        "  10.254.0.10| 40000|  10.254.0.22|  5201|      12|   17664|    1\n"
    )
    rows = api.netflow_query(start_time="-600", exporter="chinng")

    assert rows == [
        {
            "first_seen": "2026-10-07 15:56:13.433",
            "last_seen": "2026-10-07 15:56:28.001",
            "exporter": "chinng",
            "in_if": 2,
            "proto": "udp",
            "src_ip": "10.254.0.10",
            "src_port": 40000,
            "dst_ip": "10.254.0.22",
            "dst_port": 5201,
            "packets": 12,
            "bytes": 17664,
            "flows": 1,
        }
    ]
    assert _nfdump_argv(api)[-1] == "router ip 172.28.0.3"


def test_filter_stays_one_nfdump_argument() -> None:
    api = _FakeCollector("No matching flows\n")
    hostile = "proto udp' ; rm -rf / ; echo '"

    assert api.netflow_query(start_time="-60", filter=hostile) == []
    assert _nfdump_argv(api)[-1] == f"({hostile})"


def test_aggregation_uses_whitelisted_keys() -> None:
    api = _FakeCollector("t0|t1|172.28.0.6|3|100|150000|4\n")
    rows = api.netflow_query(
        start_time="-60", aggregate_by=["router", "inif"], order_by="packets", limit=500
    )

    argv = _nfdump_argv(api)
    assert argv[argv.index("-A") + 1] == "router,inif"
    assert argv[argv.index("-O") + 1] == "packets"
    # The agent's limit reaches nfdump as is.
    assert argv[argv.index("-n") + 1] == "500"
    assert rows[0]["exporter"] == "iplsng"
    assert rows[0]["in_if"] == 3


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"aggregate_by": ["srcmac"]}, "Unknown aggregate_by"),
        ({"order_by": "duration"}, "order_by"),
        ({"exporter": "nowhere"}, "Unknown exporter"),
        ({"limit": 0}, "limit must be"),
    ],
)
def test_invalid_arguments_are_rejected(kwargs: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _FakeCollector("").netflow_query(start_time="-60", **kwargs)


def test_nfdump_errors_are_reported() -> None:
    api = _FakeCollector("Line 1: Unknown protocol at 'bogus'\n")
    with pytest.raises(ValueError, match="Unknown protocol"):
        api.netflow_query(start_time="-60", filter="proto bogus")
