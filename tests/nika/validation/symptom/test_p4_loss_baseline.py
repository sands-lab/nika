"""Loss controls cover the declared egress and an independent path."""

from types import SimpleNamespace

import pytest

from nika.net_env.p4_dc_gateway.topology_model import build_gateway_fabric_model
from nika.validation.symptom import p4_gateway_probes as probes

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("size", ["s", "m", "l"])
@pytest.mark.parametrize("role", ["gateway", "spine"])
@pytest.mark.parametrize("missing_control", [False, True])
def test_loss_baseline_covers_target_peer_and_requires_independent_control(
    monkeypatch, size, role, missing_control
):
    model = build_gateway_fabric_model(size)
    switch = f"{role}_1"
    peer = "spine_2" if role == "gateway" else "leaf_2"
    port = model.port_to_peer(switch, peer)
    selected = []
    problem = SimpleNamespace(
        net_env=SimpleNamespace(model=model),
        runtime=SimpleNamespace(exec=lambda *_args, **_kwargs: ""),
    )
    monkeypatch.setattr(probes.time, "sleep", lambda _: None)
    monkeypatch.setattr(probes, "_clock_ns", lambda _: 0)

    def send(_problem, state):
        selected.extend(service["name"] for service in state["services"])
        return set(range(20000, 20000 + probes._LOSS_FLOWS * len(selected)))

    def reports(_problem, _start, destination, _port):
        service = next(item for item in model.services if item.ip == destination)
        actual_port = (
            model.port_to_peer(switch, service.attached_switch).bmv2_port
            if role == "spine"
            else port.bmv2_port
        )
        if missing_control:
            actual_port = port.bmv2_port
        elif role == "gateway":
            return [
                {
                    "src_port": source,
                    "hop_sequence": [
                        {
                            "switch_id": model.switch_info[switch].device_id,
                            "egress_port": (
                                actual_port
                                if offset == 0
                                else model.port_to_peer(switch, "spine_1").bmv2_port
                            ),
                        }
                    ],
                }
                for offset, source in enumerate((20000, 20001))
            ]
        return [
            {
                "src_port": 20000 + model.services.index(service),
                "hop_sequence": [
                    {
                        "switch_id": model.switch_info[switch].device_id,
                        "egress_port": actual_port,
                    }
                ],
            }
        ]

    monkeypatch.setattr(probes, "_loss_round", send)
    monkeypatch.setattr(probes, "_reports_since", reports)
    ok, evidence = probes.silent_loss_baseline(
        problem, SimpleNamespace(host_name=switch, bmv2_port=port.bmv2_port)
    )
    assert selected == (
        ["service_2_1", "service_1_1"] if role == "spine" else ["service_1_1"]
    )
    assert evidence["flows_on_port"] > 0
    assert ok is not missing_control


@pytest.mark.parametrize(
    "arrived,expected",
    [({2, 3, 4}, True), ({1, 2, 3, 4}, False), ({2, 4}, False)],
)
def test_loss_observation_keeps_service_inventory_and_independent_loss_control(
    monkeypatch, arrived, expected
):
    services = [
        {"name": "service_2_1", "ip": "10.0.2.11"},
        {"name": "service_1_1", "ip": "10.0.1.11"},
    ]
    problem = SimpleNamespace(
        root_cause_name="silent_egress_packet_loss",
        _audit_loss={
            "client": "client_1",
            "services": services,
            "on_port": [1, 2],
            "elsewhere": [3, 4],
        },
    )
    monkeypatch.setattr(probes, "_loss_round", lambda *_: arrived)
    ok, evidence = probes.silent_loss(problem, SimpleNamespace())
    assert ok is expected
    assert evidence["verified"] is expected
    assert evidence["details"]["services"] == services
    assert evidence["details"]["flows_on_port"] == 2
    assert evidence["details"]["flows_elsewhere"] == 2
