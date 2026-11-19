from types import SimpleNamespace

import pytest

from nika.validation.symptom import custom

pytestmark = pytest.mark.unit


def _problem(monkeypatch, losses):
    it = iter(losses)

    def sample(*_):
        loss = next(it)
        return {
            "loss_percent": loss,
            "intervals": [
                {
                    "time": str(i),
                    "rx": round((100 - loss) * 100),
                    "lost": round(loss * 100),
                }
                for i in range(3)
            ],
        }

    monkeypatch.setattr(
        custom,
        "_rtp_quality",
        sample,
    )
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


@pytest.mark.parametrize("rx,lost,expected", [(0, 10, False), (2300, 1600, True)])
def test_degradation_requires_received_media(monkeypatch, rx, lost, expected):
    sample = {
        "loss_percent": 100.0 * lost / (rx + lost),
        "intervals": [{"time": str(i), "rx": rx, "lost": lost} for i in range(3)],
    }
    monkeypatch.setattr(custom, "_rtp_quality", lambda *_: sample)
    problem = SimpleNamespace(
        root_cause_name="vrf_dscp_remarking",
        _audit_rtp_baseline=0.0,
        _audit_rtp_sham=0.0,
    )
    verified, evidence = custom._vrf_dscp_remarking(problem, None)
    assert verified is expected
    if not rx:
        assert evidence["details"]["current"]["error"] == "no_received_media"


@pytest.mark.parametrize(
    "losses,times,expected",
    [
        ([80, 0, 0], [0, 1, 2], False),
        ([40, 40], [0, 1], False),
        ([40, 40, 40], [0, 0, 1], False),
        ([0, 40, 40, 40], [0, 1, 2, 3], True),
    ],
)
def test_degradation_uses_three_distinct_sustained_intervals(
    monkeypatch, losses, times, expected
):
    sample = {
        "loss_percent": sum(losses) / len(losses),
        "intervals": [
            {"time": str(t), "rx": 100 - loss, "lost": loss}
            for loss, t in zip(losses, times, strict=True)
        ],
    }
    monkeypatch.setattr(custom, "_rtp_quality", lambda *_: sample)
    problem = SimpleNamespace(
        root_cause_name="vrf_dscp_remarking",
        _audit_rtp_baseline=0.0,
        _audit_rtp_sham=0.0,
    )
    assert custom._vrf_dscp_remarking(problem, None)[0] is expected
