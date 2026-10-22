from types import SimpleNamespace

import pytest

from nika.validation.symptom import custom

pytestmark = pytest.mark.unit


def _problem(monkeypatch, losses):
    it = iter(losses)
    monkeypatch.setattr(custom, "_rtp_quality", lambda *_: {"loss_percent": next(it)})
    return SimpleNamespace(root_cause_name="vrf_dscp_remarking")


def test_baseline_remeasures_until_settled(monkeypatch):
    problem = _problem(monkeypatch, [10.3, 1.0])
    ok, sample = custom._vrf_dscp_baseline(problem, None)
    assert ok and problem._audit_rtp_baseline == 1.0
    assert sample["attempt_loss_percent"] == [10.3, 1.0]


def test_baseline_unreliable_when_never_settles(monkeypatch):
    problem = _problem(monkeypatch, [9.0, 8.0, 7.0])
    ok, sample = custom._vrf_dscp_baseline(problem, None)
    assert not ok and not sample["baseline_reliable"]


def test_delta_uses_lower_of_baseline_and_sham(monkeypatch):
    problem = _problem(monkeypatch, [4.0, 0.5, 15.5])
    custom._vrf_dscp_baseline(problem, None)
    assert custom._vrf_dscp_remarking(problem, None)[0] is False  # sham
    problem._audit_rtp_baseline = 10.3  # noisy baseline
    ok, _ = custom._vrf_dscp_remarking(problem, None)
    assert ok  # 15.5 - min(10.3, 0.5) >= 8
