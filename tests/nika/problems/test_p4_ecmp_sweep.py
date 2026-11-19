"""ECMP sweep checks distinguish test packets from other port traffic."""

from types import SimpleNamespace

import pytest

from nika.validation.symptom import p4_runtime_probes as probes

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "member_packets,delivered,other_packets,expected",
    [(0, 256, 64, True), (1, 256, 64, False), (0, 200, 64, False), (0, 256, 0, False)],
)
def test_ecmp_uses_filtered_member_packets_and_retains_controls(
    monkeypatch, member_packets, delivered, other_packets, expected
):
    ends = {"member_port": 5, "other_ports": [6, 7, 8]}
    sample = {
        "flows": 256,
        "delivered": delivered,
        "member_probe_packets": member_packets,
        "next_hop_delta": {5: 7, 6: other_packets, 7: 75, 8: 65},
    }
    monkeypatch.setattr(probes, "_faulted_sweep", lambda *_, **kw: (ends, sample))
    problem = SimpleNamespace(root_cause_name="p4_ecmp_group_member_missing")
    assert probes.ecmp_member_missing(problem, None)[0] is expected


def test_ecmp_baseline_requires_filtered_packets_on_member(monkeypatch):
    ends = {"member_port": 5, "other_ports": [6, 7, 8]}
    sample = {
        "flows": 256,
        "delivered": 256,
        "member_probe_packets": 0,
        "egress_delta": {31: 0},
        "next_hop_delta": {5: 7, 6: 64, 7: 64, 8: 64},
    }
    monkeypatch.setattr(probes, "_sweep_ends", lambda *_: ends)
    monkeypatch.setattr(probes, "_sweep", lambda *_, **kw: sample)
    problem = SimpleNamespace()
    assert not probes.ecmp_sweep_baseline(problem, None)[0]
    sample["member_probe_packets"] = 64
    assert probes.ecmp_sweep_baseline(problem, None)[0]
