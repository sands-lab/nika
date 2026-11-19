"""The faulted throughput probe allows slow transfers to finish."""

import json
from unittest.mock import Mock

import pytest

from nika.net_env import verify
from nika.validation.symptom import perf_probes

pytestmark = pytest.mark.unit


def test_iperf_deadline_keeps_receiver_measurement_and_default(monkeypatch):
    monkeypatch.setattr(verify, "_start_iperf3_server", lambda *args: "42")
    stop = Mock()
    monkeypatch.setattr(verify, "_stop_iperf3_server", stop)
    runtime = Mock()
    runtime.exec.return_value = json.dumps(
        {
            "end": {
                "sum_received": {"bits_per_second": 6000},
                "sum_sent": {"retransmits": 2},
            }
        }
    )
    assert verify.iperf_throughput_bps(runtime, "src", "dst", "10.0.0.1") == 6000
    assert runtime.exec.call_args.kwargs["timeout"] == 18
    assert verify.iperf_tcp_metrics(
        runtime, "src", "dst", "10.0.0.1", timeout_sec=180
    ) == (6000, 2)
    assert runtime.exec.call_args.kwargs["timeout"] == 180
    assert stop.call_count == 2


def test_iperf_median_requires_all_three_measurements(monkeypatch):
    probe = Mock(side_effect=[6000, None, 8000])
    monkeypatch.setattr(perf_probes, "iperf_throughput_bps", probe)
    result = perf_probes._iperf_median(None, "src", "dst", "10.0.0.1", timeout_sec=180)
    assert result == {"runs_bps": [6000, None, 8000], "median_bps": None}
    assert all(call.kwargs["timeout_sec"] == 180 for call in probe.call_args_list)
