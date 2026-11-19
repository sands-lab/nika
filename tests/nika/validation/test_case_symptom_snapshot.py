"""Restored task checks retain every member's healthy baseline and placement."""

from types import SimpleNamespace

import pytest

from nika.problems.combinations import get_combination, task_placement
from nika.problems.multi import MultiFaultProblem
from nika.validation.symptom import combinations, snapshot
from nika.workflows.benchmark.load_config import normalize_benchmark_row

pytestmark = pytest.mark.contract


@pytest.mark.parametrize(
    "name",
    ["redundant_links_both_lost", "latent_adjacency_cut", "selector_masks_readiness"],
)
def test_restored_combination_uses_each_baseline_and_live_instance(monkeypatch, name):
    row = normalize_benchmark_row(get_combination(name).rows()[0])
    placement = task_placement(row)
    faults = {}
    for fault, inject in placement.items():
        faults[fault] = SimpleNamespace(
            net_env=SimpleNamespace(topo_size="s"),
            scenario_name=row["scenario"],
            runtime=object(),
            root_cause_name=fault.split("[")[0],
            recheck_artifact=lambda params: {"present": True},
        )
    monkeypatch.setattr(
        "nika.utils.session.Session",
        lambda: SimpleNamespace(load_running_session=lambda **kw: object()),
    )
    monkeypatch.setattr(
        "nika.workflows.failure.inject.prepare_failure",
        lambda session, names, inject: SimpleNamespace(
            problem=faults[
                next(
                    k
                    for k, value in placement.items()
                    if value == inject and faults[k].root_cause_name == names[0]
                )
            ],
            params=inject,
        ),
    )
    monkeypatch.setattr(
        snapshot, "get_symptom_contract", lambda fault: SimpleNamespace(probe="custom")
    )

    def baseline(fault, problem, params):
        problem._audit_marker = dict(params)
        return True, {"healthy": fault}

    monkeypatch.setattr(
        "nika.validation.symptom.custom.evaluate_custom_baseline", baseline
    )
    snapshot.capture_task_baseline("source", row)
    for problem in faults.values():
        problem._audit_marker = "overwritten"

    def symptom(runtime, fault, params, **kwargs):
        label = next(
            k
            for k, value in placement.items()
            if value == params and faults[k].root_cause_name == fault
        )
        assert kwargs["problem"] is faults[label]
        assert kwargs["problem"].root_cause_name == fault
        assert kwargs["problem"]._audit_marker == params
        return name != "selector_masks_readiness", {"fault": fault}

    monkeypatch.setattr("nika.validation.symptom.evaluate.evaluate_symptom", symptom)
    if row.get("schedule"):
        prepared = {
            (i, 0): SimpleNamespace(problem=faults[e["problem"]], params=e["inject"])
            for i, e in enumerate(row["schedule"]["events"])
        }
        scheduler = SimpleNamespace(_prepared=prepared, _active=dict.fromkeys(prepared))
        bound = None
    else:
        problem = object.__new__(MultiFaultProblem)
        problem.sub_faults = list(faults.values())
        problem.problem_names = list(faults)
        bound = (problem, SimpleNamespace(sub_params=placement))
        scheduler = None
    monkeypatch.setattr(snapshot, "bound_injected_problem", lambda sid: bound)
    monkeypatch.setattr("nika.workflows.schedule.scheduler_for", lambda sid: scheduler)

    def relation(ctx):
        assert ctx.problems == faults
        assert ctx.inject == placement
        return name != "selector_masks_readiness", {}

    monkeypatch.setitem(
        combinations.CHECKS,
        name,
        combinations.Check(
            "masked" if name == "selector_masks_readiness" else "joint",
            relation,
        ),
    )
    result = snapshot.evaluate_on_snapshot("restored", "source")
    assert result["ok"]
    assert len(result["payload"]["members"]) == 2
    assert result["payload"]["relation"]["combination"] == name
    monkeypatch.setitem(
        combinations.CHECKS, name, combinations.Check("joint", lambda ctx: (False, {}))
    )
    assert not snapshot.evaluate_on_snapshot("restored", "source")["ok"]
    monkeypatch.setitem(
        combinations.CHECKS,
        name,
        combinations.Check(
            "masked" if name == "selector_masks_readiness" else "joint",
            lambda ctx: (name != "selector_masks_readiness", {}),
        ),
    )
    faults[next(iter(faults))].recheck_artifact = lambda params: {"present": False}
    monkeypatch.setattr("nika.validation.presence.time.sleep", lambda _: None)
    assert not snapshot.evaluate_on_snapshot("restored", "source")["ok"]


def test_redundant_ospf_member_measures_adjacency_and_declared_path(monkeypatch):
    from nika.validation.symptom import routing_probes

    monkeypatch.setattr(
        "nika.problems.routing_control_plane.ospf.ospf_stub_router", lambda *args: None
    )
    monkeypatch.setattr(
        "nika.net_env.net_env_pool.get_probe_path",
        lambda *args, **kw: SimpleNamespace(src_host="client", dst_ip="192.0.2.2"),
    )
    health = {
        node: {"full": True, "interface_up": True, "routes": []}
        for node in ("router", "peer")
    }
    loss = [0.0]
    monkeypatch.setattr(
        routing_probes,
        "ping_stats",
        lambda *args, **kw: SimpleNamespace(loss_percent=loss[0]),
    )
    problem = SimpleNamespace(
        net_env=SimpleNamespace(topo_size="s"),
        scenario_name="environment",
        runtime=object(),
        link_health=lambda params: health,
        root_cause_name="ospf_hello_interval_mismatch",
    )
    params = SimpleNamespace(host_name="router", intf_name="eth0")
    ok, before = routing_probes.ospf_cut_edge_baseline(problem, params)
    assert ok and before["stub"] is None
    assert before["src"] == "client"
    for row in health.values():
        row["full"] = False
    loss[0] = 100.0
    ok, after = routing_probes._ospf_cut_edge_lost(problem, params, timeout=0)
    assert ok and after["details"]["loss"] == 100.0


def test_changed_service_masks_readiness_without_waiting_for_pod_timeout(monkeypatch):
    from nika.validation.symptom import k8s_probes

    problem = SimpleNamespace(
        root_cause_name="k8s_readiness_probe_misconfig",
        _audit_readiness_invariants={"services": {"selector": "healthy"}},
    )
    monkeypatch.setattr(
        k8s_probes,
        "_readiness_invariants",
        lambda *args: {"services": {"selector": "changed"}},
    )

    def cannot_wait(*args, **kwargs):
        raise AssertionError("The required configuration invariant is already false")

    monkeypatch.setattr(k8s_probes, "_wait_for", cannot_wait)
    observed, evidence = k8s_probes.readiness_broken(problem, object())
    assert observed is False
    assert evidence["details"]["services_and_ingresses_unchanged"] is False
