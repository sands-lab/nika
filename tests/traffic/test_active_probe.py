from __future__ import annotations

from unittest.mock import Mock, patch

from traffic.active_probe import run_active_tcp_probe


def test_active_probe_uses_requested_five_tuple_and_seeded_payload() -> None:
    runtime = Mock()
    runtime.get_data_plane_host_ip.return_value = "10.0.1.2"
    runtime.exec.return_value = "{'acked': 2, 'elapsed_ms': 1.0}"
    with patch("traffic.active_probe.time.sleep"):
        result = run_active_tcp_probe(
            runtime,
            source="pc_0_0",
            destination="pc_0_1",
            source_port=21001,
            destination_port=5201,
            payload_seed=9,
            packets=2,
        )
    assert result["source_port"] == 21001
    assert result["destination_port"] == 5201
    assert runtime.exec.call_count == 2
