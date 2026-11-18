"""A MAC conflict is read from the shared ARP answer and the pair's loss."""

from types import SimpleNamespace

import pytest

from nika.validation.symptom import addressing_probes

pytestmark = pytest.mark.unit

ENDS = {
    "observer": "obs",
    "owner": "a",
    "owner_ip": "10.0.0.3",
    "claimant": "b",
    "claimant_ip": "10.0.0.4",
    "control": "c",
    "control_ip": "10.0.0.2",
}


def _verify(monkeypatch, losses, macs):
    reading = {
        "owner_loss_percent": losses[0],
        "claimant_loss_percent": losses[1],
        "control_loss_percent": 0.0,
    }
    monkeypatch.setattr(
        addressing_probes, "_mac_conflict_reading", lambda p, e: reading
    )
    monkeypatch.setattr(
        addressing_probes,
        "_holder_macs",
        lambda p, e: {"owner_mac": macs[0], "claimant_mac": macs[1]},
    )
    problem = SimpleNamespace(
        root_cause_name="mac_address_conflict", _audit_mac_ends=ENDS
    )
    return addressing_probes.mac_conflict(problem, None)[0]


def test_bridge_held_on_one_port_still_shows_the_conflict(monkeypatch):
    shared = ("4e:40:f9:9b:45:53", "4e:40:f9:9b:45:53")
    assert _verify(monkeypatch, (0.0, 100.0), shared)
    assert _verify(monkeypatch, (48.0, 52.0), shared)


def test_one_unreachable_host_is_not_a_conflict(monkeypatch):
    assert not _verify(monkeypatch, (0.0, 100.0), ("4e:40:f9:9b:45:53", None))
    assert not _verify(
        monkeypatch, (0.0, 100.0), ("4e:40:f9:9b:45:53", "3a:a1:07:08:53:98")
    )
