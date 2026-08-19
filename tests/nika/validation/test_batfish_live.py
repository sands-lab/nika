from __future__ import annotations

from pathlib import Path

import pytest

from nika.net_env.contract import ValidationReport
from nika.net_env.kathara.isp.isp.lab import Isp
from nika.validation.batfish.service import ensure_batfish_service
from nika.validation.batfish.snapshot import build_isp_snapshot
from nika.validation.batfish.verifier import BatfishVerifier
from nika.workflows.env.start import start_net_env
from nika.workflows.session.close import close_session
from tests.support.prerequisites import docker_available


pytestmark = pytest.mark.skipif(not docker_available(), reason="Docker not available")


def _verify(env: Isp, root: Path, configs: dict[str, str] | None = None):
    ensure_batfish_service()
    snapshot = build_isp_snapshot(
        root=root,
        contract=env.validation_contract,
        plan=env.plan,
        traffic=env.traffic,
        deployment_configs=configs or env.deployment_configs,
    )
    return BatfishVerifier().verify(env.validation_contract, snapshot)


def test_live_batfish_ospf_healthy_and_area_mutation(tmp_path) -> None:
    env = Isp(topo="abilene", igp="ospf", bgp_mode="none")
    healthy = _verify(env, tmp_path / "healthy")
    assert healthy.status == "passed"

    intent = next(
        item
        for item in env.validation_contract.intents
        if item.id.startswith("adj.ospf")
    )
    assert intent.adjacency is not None
    adjacency = intent.adjacency
    configs = dict(env.deployment_configs)
    needle = f"network {adjacency.local_address}/31 area 0.0.0.0"
    configs[adjacency.local_node] = configs[adjacency.local_node].replace(
        needle,
        f"network {adjacency.local_address}/31 area 0.0.0.1",
        1,
    )
    mutated = _verify(env, tmp_path / "area-mismatch", configs)
    result = next(row for row in mutated.results if row.intent == intent.id)
    assert result.status == "failed"
    assert result.evidence["violations"][0]["Session_Status"] == "AREA_MISMATCH"

    reachability = next(
        item
        for item in env.validation_contract.intents
        if item.property == "reachability"
    )
    assert reachability.source is not None and reachability.source.node is not None
    source_node = next(
        node for node in env.plan.nodes if node.device_name == reachability.source.node
    )
    configs = dict(env.deployment_configs)
    for interface in source_node.interfaces:
        if not interface.passive:
            configs[source_node.device_name] = configs[source_node.device_name].replace(
                f" ip address {interface.address}/{interface.prefixlen}\n", "", 1
            )
    reachability_report = _verify(env, tmp_path / "reachability-broken", configs)
    reachability_result = next(
        row for row in reachability_report.results if row.intent == reachability.id
    )
    assert reachability_result.status == "failed"
    assert "NO_ROUTE" in str(reachability_result.evidence["violations"])

    isolation = next(
        item for item in env.validation_contract.intents if item.property == "isolation"
    )
    assert isolation.source is not None and isolation.source.node is not None
    configs = dict(env.deployment_configs)
    configs[isolation.source.node] = configs[isolation.source.node].replace(
        "interface lo\n", "interface lo\n ip address 192.0.2.1/24\n", 1
    )
    isolation_report = _verify(env, tmp_path / "isolation-broken", configs)
    isolation_result = next(
        row for row in isolation_report.results if row.intent == isolation.id
    )
    assert isolation_result.status == "failed"
    assert "ACCEPTED" in str(isolation_result.evidence["violations"])

    waypoint = next(
        item for item in env.validation_contract.intents if item.property == "waypoint"
    )
    selected_edges = {
        frozenset(pair)
        for pair in (
            ("atlam5", "atlang"),
            ("atlang", "iplsng"),
            ("iplsng", "chinng"),
            ("chinng", "nycmng"),
            ("nycmng", "washng"),
        )
    }
    interface_costs = {node.device_name: {} for node in env.plan.nodes}
    for link in env.plan.links:
        cost = (
            1
            if frozenset((link.endpoint_a, link.endpoint_b)) in selected_edges
            else 1000
        )
        interface_costs[link.endpoint_a][link.iface_a] = cost
        interface_costs[link.endpoint_b][link.iface_b] = cost
    configs = {
        device: _replace_ospf_costs(content, interface_costs[device])
        for device, content in env.deployment_configs.items()
    }
    waypoint_report = _verify(env, tmp_path / "waypoint-broken", configs)
    waypoint_result = next(
        row for row in waypoint_report.results if row.intent == waypoint.id
    )
    assert waypoint_result.status == "failed"
    assert "chinng" in str(waypoint_result.evidence["violations"])


def test_live_batfish_bgp_baseline_and_remote_as_mutation(tmp_path) -> None:
    env = Isp(topo="abilene", igp="ospf", bgp_mode="ebgp")
    assert env.bgp_plan is not None
    healthy = _verify(env, tmp_path / "healthy-ebgp")
    assert healthy.status == "passed"
    assert all(result.status == "passed" for result in healthy.results)
    forwarding_loops = next(
        check for check in healthy.sanity if check.check == "forwarding_loops"
    )
    assert forwarding_loops.status == "passed"
    session = next(
        item for item in env.bgp_plan.sessions if item.session_type == "ebgp"
    )
    intent = next(
        item
        for item in env.validation_contract.intents
        if item.adjacency is not None
        and item.adjacency.protocol == "bgp"
        and item.adjacency.local_node == session.local_device
        and item.adjacency.remote_address == session.remote_ip
    )
    configs = dict(env.deployment_configs)
    needle = f"neighbor {session.remote_ip} remote-as {session.remote_asn}"
    configs[session.local_device] = configs[session.local_device].replace(
        needle,
        f"neighbor {session.remote_ip} remote-as {session.remote_asn + 100}",
        1,
    )
    report = _verify(env, tmp_path / "remote-as-mismatch", configs)
    result = next(row for row in report.results if row.intent == intent.id)
    assert result.status == "failed"
    assert result.evidence["violations"][0]["Configured_Status"] == "HALF_OPEN"


def test_live_batfish_rpki_profile_marks_flow_modeling_unsupported(tmp_path) -> None:
    env = Isp(topo="abilene", igp="ospf", bgp_mode="ebgp", rpki=True)
    report = _verify(env, tmp_path / "rpki")
    flow_results = [
        result
        for result, intent in zip(report.results, env.validation_contract.intents)
        if intent.property != "adjacency"
    ]
    adjacency_results = [
        result
        for result, intent in zip(report.results, env.validation_contract.intents)
        if intent.property == "adjacency"
    ]
    assert flow_results
    assert all(result.status == "unsupported" for result in flow_results)
    assert all(result.status == "passed" for result in adjacency_results)
    forwarding_loops = next(
        check for check in report.sanity if check.check == "forwarding_loops"
    )
    assert forwarding_loops.status == "passed"


def test_kathara_ospf_static_then_runtime_pipeline(tmp_path) -> None:
    session_id = None
    try:
        session_id = start_net_env(
            "isp",
            None,
            topo="pdh",
            igp="ospf",
            bgp_mode="none",
            result_dir=str(tmp_path),
            session_tag="batfish-live",
            static_validation=True,
        )
        from nika.utils.session_store import SessionStore

        row = SessionStore().get_session(session_id)
        artifact_dir = Path(row["session_dir"])
        static_report = ValidationReport.load(artifact_dir / row["validation_batfish"])
        runtime_report = ValidationReport.load(artifact_dir / row["validation_results"])
        assert static_report.status == "passed"
        assert runtime_report.status == "passed"
        assert {result.intent for result in static_report.results} == {
            result.intent for result in runtime_report.results
        }
    finally:
        if session_id is not None:
            close_session(session_id=session_id)


def test_kathara_ospf_defaults_to_runtime_validation_only(tmp_path) -> None:
    session_id = None
    try:
        session_id = start_net_env(
            "isp",
            None,
            topo="pdh",
            igp="ospf",
            bgp_mode="none",
            result_dir=str(tmp_path),
            session_tag="runtime-live",
        )
        from nika.utils.session_store import SessionStore

        row = SessionStore().get_session(session_id)
        artifact_dir = Path(row["session_dir"])
        runtime_report = ValidationReport.load(artifact_dir / row["validation_results"])
        assert runtime_report.status == "passed"
        assert "validation_batfish" not in row
        assert not (artifact_dir / "batfish-validation.json").exists()
        assert not (artifact_dir / "batfish-snapshot").exists()
    finally:
        if session_id is not None:
            close_session(session_id=session_id)


def _replace_ospf_costs(content: str, costs: dict[str, int]) -> str:
    current_interface = None
    output: list[str] = []
    for line in content.splitlines(keepends=True):
        if line.startswith("interface "):
            current_interface = line.split()[1]
        if line.strip().startswith("ip ospf cost ") and current_interface in costs:
            line = f" ip ospf cost {costs[current_interface]}\n"
        output.append(line)
    return "".join(output)
