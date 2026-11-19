"""Selector symptoms distinguish probe packets from unrelated port traffic."""

from types import SimpleNamespace

import pytest

from nika.validation.symptom import p4_runtime_probes as probes

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "probe_packets,bad_packets,delivered,control_packets,expected",
    [
        (0, 63, 193, 65, True),
        (1, 63, 193, 65, False),
        (0, 0, 193, 65, False),
        (0, 63, 256, 65, False),
        (0, 63, 193, 0, False),
    ],
)
def test_selector_requires_probe_absence_and_bad_port_loss(
    monkeypatch, probe_packets, bad_packets, delivered, control_packets, expected
):
    ends = {"member_port": 5, "other_ports": [6, 7, 8]}
    sample = {
        "flows": 256,
        "delivered": delivered,
        "member_probe_packets": probe_packets,
        "egress_delta": {5: 4, 6: control_packets, 7: 75, 8: 65, 31: bad_packets},
    }
    monkeypatch.setattr(probes, "_faulted_sweep", lambda *_: (ends, sample))
    problem = SimpleNamespace(root_cause_name="p4_action_selector_member_misconfig")
    assert probes.selector_member_blackhole(problem, None)[0] is expected


def test_selector_baseline_requires_probe_on_original_port(monkeypatch):
    ends = {"member_port": 5, "other_ports": [6, 7, 8]}
    sample = {
        "flows": 256,
        "delivered": 256,
        "member_probe_packets": 0,
        "egress_delta": {5: 4, 6: 64, 7: 64, 8: 64, 31: 0},
    }
    monkeypatch.setattr(probes, "_sweep_ends", lambda *_: ends)
    monkeypatch.setattr(probes, "_sweep", lambda *_, **kw: sample)
    problem = SimpleNamespace()
    assert not probes.selector_sweep_baseline(problem, None)[0]
    sample["member_probe_packets"] = 64
    assert probes.selector_sweep_baseline(problem, None)[0]
