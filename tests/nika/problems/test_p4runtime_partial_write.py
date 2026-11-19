"""Partial-write status and reconstruction contracts for both P4 environments."""

import copy
from unittest.mock import Mock

import pytest

from nika.net_env.p4_dc_fabric.fabric_manager import apply
from nika.net_env.p4_dc_fabric.fabric_manager.intent import build_forwarding_intent
from nika.net_env.p4_dc_fabric.topology_model import build_clos_fabric_model
from nika.net_env.p4_dc_gateway.control import build_gateway_intent
from nika.net_env.p4_dc_gateway.topology_model import build_gateway_fabric_model
from nika.problems.forwarding_encapsulation_policy import p4_runtime as faults

pytestmark = pytest.mark.unit


@pytest.fixture(params=["p4_dc_fabric", "p4_dc_gateway"])
def intent(request):
    def build(size):
        if request.param == "p4_dc_fabric":
            return build_forwarding_intent(build_clos_fabric_model(size)), "leaf_1"
        model = build_gateway_fabric_model(size)
        return build_gateway_intent(model), model.backend_pool[0].attached_switch

    return build


def problem():
    instance = object.__new__(faults.P4RuntimePartialWrite)
    instance.runtime = Mock()
    instance._pair = None
    return instance


@pytest.mark.parametrize("size", ["s", "m", "l"])
def test_reconstructed_and_original_instance_agree_on_fault_and_repair(
    monkeypatch, intent, size
):
    original, switch = intent(size)
    pair = faults.partial_write_routes(original, switch)
    revised = faults.partial_write_intent(original, pair)
    observed = copy.deepcopy(revised)
    observed["switches"][switch]["ipv4_lpm"] = copy.deepcopy(
        original["switches"][switch]["ipv4_lpm"]
    )
    monkeypatch.setattr(faults, "load_intent", lambda _: copy.deepcopy(revised))
    monkeypatch.setattr(faults, "run_manager", lambda *a, **kw: observed)
    params = faults.P4RuntimePartialWriteParams(host_name=switch)
    injecting, fresh = problem(), problem()
    injecting._pair = pair
    for instance in (injecting, fresh):
        assert instance.verify_fault(params)["verified"]
    # Both a full repair and a completed write must be rejected as fault evidence.
    for healthy in (original, revised):
        observed = healthy
        for instance in (injecting, fresh):
            assert not instance.verify_fault(params)["verified"]


@pytest.mark.parametrize(
    "codes",
    [
        ["OK", "OK", "ALREADY_EXISTS", "ALREADY_EXISTS"],
        ["OK", "OK"],
        ["OK", "OK", "ALREADY_EXISTS"],
        ["OK", "OK", "UNKNOWN", "ALREADY_EXISTS"],
        ["OK", "OK", "ALREADY_EXISTS", "INVALID_ARGUMENT"],
        ["OK", "OK", "ALREADY_EXISTS", "ALREADY_EXISTS", "ALREADY_EXISTS"],
        ["OK", "OK", "OK", "OK"],
        ["UNKNOWN", "OK", "ALREADY_EXISTS", "ALREADY_EXISTS"],
    ],
)
def test_injection_requires_exact_per_update_statuses(monkeypatch, codes):
    original = build_forwarding_intent(build_clos_fabric_model("s"))
    monkeypatch.setattr(faults, "load_intent", lambda _: original)
    monkeypatch.setattr(faults, "capture_entity", Mock(return_value={}))
    monkeypatch.setattr(apply, "_copy_in", Mock())
    monkeypatch.setattr(
        faults,
        "p4runtime_write",
        Mock(return_value={"statuses": [{"code": code} for code in codes]}),
    )
    instance = problem()
    params = faults.P4RuntimePartialWriteParams(host_name="leaf_1")
    if codes == ["OK", "OK", "ALREADY_EXISTS", "ALREADY_EXISTS"]:
        instance.inject_fault(params)
    else:
        with pytest.raises(RuntimeError, match="not partially applied"):
            instance.inject_fault(params)
