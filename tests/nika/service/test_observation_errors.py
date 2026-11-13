"""Failed observations must not become healthy empty tool results."""

from unittest.mock import Mock

import pytest

from nika.service.kathara.sdn_api import SdnAPIMixin
from nika.service.kathara.telemetry_api import TelemetryAPIMixin
from nika.service.lab.frr_api import FRRAPIMixin
from nika.service.packet_capture import inspect

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "api_type,method,kwargs",
    [
        (FRRAPIMixin, "frr_get_rpki_status", {"device": "router"}),
        (FRRAPIMixin, "frr_get_routing_state", {"device": "router"}),
        (SdnAPIMixin, "sdn_controller_logs", {}),
        (TelemetryAPIMixin, "int_query_telemetry", {"start_time": "0"}),
    ],
)
@pytest.mark.parametrize(
    "raw", ["command failed\n__rc=1", "Error executing command on node: unavailable"]
)
def test_tool_reports_failed_observation(api_type, method, kwargs, raw) -> None:
    api = api_type()
    api.exec_cmd = Mock(return_value=raw)
    with pytest.raises(RuntimeError, match="Observation"):
        getattr(api, method)(**kwargs)


@pytest.mark.parametrize("raw", ["__rc=0", "not-a-clock\n__rc=0"])
def test_int_rejects_invalid_clock(raw) -> None:
    api = TelemetryAPIMixin()
    api.exec_cmd = Mock(return_value=raw)
    with pytest.raises(RuntimeError, match="invalid timestamp"):
        api.int_query_telemetry(start_time="0")


def test_empty_int_and_controller_logs_remain_healthy() -> None:
    api = TelemetryAPIMixin()
    api.exec_cmd = Mock(return_value="1794000000000000000\n__rc=0")
    assert api.int_query_telemetry(start_time="0") == []
    api = SdnAPIMixin()
    api.exec_cmd = Mock(return_value="__rc=0")
    assert api.sdn_controller_logs() == {"host": "onos", "rows": 80, "log": ""}


@pytest.mark.parametrize(
    "raw", ["tshark: invalid display filter\n__rc=2", "Error executing command"]
)
def test_packet_count_reports_failed_tshark(raw) -> None:
    runtime = Mock()
    runtime.exec.side_effect = ["/usr/bin/tshark", raw]
    with pytest.raises(RuntimeError, match="Observation"):
        inspect.count_packets(runtime, "pc1", "/tmp/capture", "ip and (")


def test_empty_tshark_result_is_zero_packets() -> None:
    runtime = Mock()
    runtime.exec.side_effect = ["/usr/bin/tshark", "__rc=0"]
    assert inspect.count_packets(runtime, "pc1", "/tmp/capture", "tcp") == 0


def test_host_tshark_reports_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        inspect.subprocess,
        "run",
        Mock(return_value=Mock(returncode=2, stdout=b"", stderr=b"tshark: bad filter")),
    )
    with pytest.raises(RuntimeError, match="tshark: bad filter"):
        inspect._host_tshark(["-r", "-", "-Y", "ip and ("])
