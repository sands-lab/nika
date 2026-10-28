from __future__ import annotations

import pytest

from unittest.mock import Mock

from nika.traffic.active_probe import run_active_tcp_probe

pytestmark = pytest.mark.unit


def test_successful_probe_returns_acknowledgement_counts() -> None:
    runtime = Mock()
    runtime.get_data_plane_host_ip.return_value = "10.0.1.2"
    runtime.exec.side_effect = ["ready", "ready", "", '{"acked": 2, "elapsed_ms": 1.0}']
    result = run_active_tcp_probe(
        runtime,
        source="pc_0_0",
        destination="pc_0_1",
        source_port=21001,
        destination_port=5201,
        payload_seed=9,
        packets=2,
    )
    assert result["status"] == "ok"
    assert result["result"]["acked"] == 2


@pytest.mark.parametrize(
    "available,output,status",
    [
        ("", "", "unsupported"),
        ("ready", '{"error":"Connection refused"}', "failed"),
        ("ready", "Traceback: socket failed", "failed"),
    ],
)
def test_probe_failures_are_structured(
    available: str, output: str, status: str
) -> None:
    runtime = Mock()
    runtime.get_data_plane_host_ip.return_value = "10.0.1.2"
    runtime.exec.side_effect = [available, available, "", output]
    result = run_active_tcp_probe(
        runtime,
        source="a",
        destination="b",
        source_port=21001,
        destination_port=5201,
        payload_seed=1,
    )
    assert result["status"] == status
    assert "Traceback" not in str(result)
