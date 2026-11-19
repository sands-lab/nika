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


@pytest.mark.parametrize("native_control", [True, False])
def test_mac_conflict_keeps_a_distinct_native_bridge_control(
    monkeypatch, native_control
):
    from nika.problems.support import router_ops
    from nika.runtime.spec import NodeRole

    roles = {
        node: SimpleNamespace(role=role)
        for node, role in {
            "a": NodeRole.HOST,
            "b": NodeRole.HOST,
            "web": NodeRole.SERVICE,
            "gateway": NodeRole.ROUTER,
        }.items()
    }
    problem = SimpleNamespace(
        net_env=SimpleNamespace(machine_identities=roles),
        runtime=SimpleNamespace(get_host_ip=lambda *a, **k: "10.0.0.3/24"),
    )
    monkeypatch.setattr(addressing_probes, "_eth0_ip", lambda *_: "10.0.0.4")
    monkeypatch.setattr(
        addressing_probes,
        "lab_addresses",
        lambda _: {"web": [("eth0", "10.0.0.5/24"), ("eth0", "10.0.0.6/24")]},
    )
    monkeypatch.setattr(
        router_ops,
        "router_fault_ops",
        lambda *_: SimpleNamespace(
            supports=lambda operation: native_control,
            ipv4_addresses=lambda: {"10.0.0.1", "198.51.100.1"},
        ),
    )
    result = addressing_probes._mac_conflict_ends(
        problem, SimpleNamespace(host_name="b", host_name_2="a")
    )
    if native_control:
        assert (result["observer"], result["control"], result["control_ip"]) == (
            "web",
            "gateway",
            "10.0.0.1",
        )
    else:
        # Two addresses on one observer do not supply an independent control.
        assert result["error"] == "no_third_node_on_the_segment"


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


@pytest.mark.parametrize("summary", ["received", "packets received"])
def test_mac_conflict_counts_iputils_and_busybox_replies(monkeypatch, summary):
    monkeypatch.setattr(
        addressing_probes,
        "exec_or_empty",
        lambda *_args, **_kwargs: (
            f"10.0.0.3 50 {summary}\n10.0.0.4 25 {summary}\n10.0.0.2 50 {summary}\n"
        ),
    )
    assert addressing_probes._mac_conflict_reading(
        SimpleNamespace(runtime=None), ENDS
    ) == {
        "owner_loss_percent": 0.0,
        "claimant_loss_percent": 50.0,
        "control_loss_percent": 0.0,
    }
