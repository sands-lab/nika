import pytest

from nika.validation.symptom import addressing_probes as ap

pytestmark = pytest.mark.unit


def _read(losses):
    it = iter(losses)
    return lambda: next(it)


def test_healthy_read_absorbs_transient_loss(monkeypatch):
    monkeypatch.setattr(ap.time, "sleep", lambda _s: None)
    assert ap.healthy_read(_read([10.0, 0.0]), float) == (0.0, 2)


def test_healthy_read_keeps_persistent_loss(monkeypatch):
    monkeypatch.setattr(ap.time, "sleep", lambda _s: None)
    assert ap.healthy_read(_read([10.0] * ap.HEALTHY_READS), float) == (
        10.0,
        ap.HEALTHY_READS,
    )
