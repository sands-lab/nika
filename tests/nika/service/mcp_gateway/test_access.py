from __future__ import annotations

from nika.mcp.gateway.access import decide_diagnosis_access


def test_role_policy_restricts_tool_and_node_role() -> None:
    policy = {
        "tools": ["iosxr_exec", "exec_shell"],
        "node_roles": ["router"],
        "node_ids": [],
    }
    roles = {"router1": "router", "pc1": "host"}
    assert decide_diagnosis_access(
        policy=policy,
        tool_name="iosxr_exec",
        arguments={"router_name": "router1", "command": "show route"},
        node_roles=roles,
    ).allowed
    denied = decide_diagnosis_access(
        policy=policy,
        tool_name="exec_shell",
        arguments={"host_name": "pc1", "command": "hostname"},
        node_roles=roles,
    )
    assert not denied.allowed
    assert denied.reason == "node_not_allowed"
    assert not decide_diagnosis_access(
        policy=policy,
        tool_name="routeros_exec",
        arguments={"router_name": "router1", "command": "/ip route print"},
        node_roles=roles,
    ).allowed


def test_vendor_cli_checks_target_node() -> None:
    decision = decide_diagnosis_access(
        policy={"tools": ["routeros_exec"], "node_roles": ["router"], "node_ids": []},
        tool_name="routeros_exec",
        arguments={"router_name": "pc1", "command": "/ip route print"},
        node_roles={"pc1": "host"},
    )
    assert not decision.allowed
    assert decision.reason == "node_not_allowed"


def test_explicit_node_id_is_an_exception_to_role_range() -> None:
    decision = decide_diagnosis_access(
        policy={"tools": ["exec_shell"], "node_roles": ["router"], "node_ids": ["pc1"]},
        tool_name="exec_shell",
        arguments={"host_name": "pc1", "command": "hostname"},
        node_roles={"pc1": "host"},
    )
    assert decision.allowed


_RESTRICTED = {"tools": ["*"], "node_roles": ["host"], "node_ids": []}
_ROLES = {"pc1": "host", "pc2": "host", "router1": "router", "onos": "controller"}


def test_every_registered_tool_declares_its_targets() -> None:
    """Unmapped tools are denied under restricted policies, so map them all."""
    from importlib import import_module

    from nika.mcp.gateway.access import TOOL_NODE_ARGUMENTS
    from nika.mcp.registry import MCP_SERVER_SPECS, SUBMISSION_SERVER

    for name, spec in MCP_SERVER_SPECS.items():
        if name == SUBMISSION_SERVER:
            continue
        mcp = import_module(spec.module).mcp
        for tool in mcp._tool_manager.list_tools():
            assert tool.name in TOOL_NODE_ARGUMENTS, (name, tool.name)
            for arg in TOOL_NODE_ARGUMENTS[tool.name]:
                assert arg in tool.parameters["properties"], (tool.name, arg)


def test_restricted_policy_fails_closed_for_unmapped_tool() -> None:
    decision = decide_diagnosis_access(
        policy=_RESTRICTED, tool_name="new_tool", arguments={}, node_roles=_ROLES
    )
    assert (decision.allowed, decision.reason) == (False, "tool_targets_unknown")
    assert decide_diagnosis_access(
        policy={"tools": ["*"], "node_roles": ["*"], "node_ids": []},
        tool_name="new_tool",
        arguments={},
        node_roles=_ROLES,
    ).allowed


def test_list_arguments_and_implicit_targets_are_checked() -> None:
    assert decide_diagnosis_access(
        policy=_RESTRICTED,
        tool_name="run_pingmesh_snapshot",
        arguments={"sources": ["pc1"], "targets": ["pc2"]},
        node_roles=_ROLES,
    ).allowed
    denied = decide_diagnosis_access(
        policy=_RESTRICTED,
        tool_name="run_pingmesh_snapshot",
        arguments={"sources": ["pc1"], "targets": ["pc2", "router1"]},
        node_roles=_ROLES,
    )
    assert denied.reason == "node_not_allowed"
    omitted = decide_diagnosis_access(
        policy=_RESTRICTED,
        tool_name="run_pingmesh_snapshot",
        arguments={"sources": ["pc1"]},
        node_roles=_ROLES,
    )
    assert omitted.reason == "explicit_targets_required"
    implicit = decide_diagnosis_access(
        policy=_RESTRICTED,
        tool_name="sdn_onos_rest",
        arguments={},
        node_roles=_ROLES,
    )
    assert (implicit.allowed, implicit.targets) == (False, ("onos",))


def test_ip_targets_pass_default_policy_and_fail_restricted() -> None:
    from nika.run_config.schema import DiagnosisAccessPolicy

    default = DiagnosisAccessPolicy().model_dump()
    args = {"source": "pc1", "destination": "10.0.0.2"}
    assert decide_diagnosis_access(
        policy=default, tool_name="active_tcp_probe", arguments=args, node_roles=_ROLES
    ).allowed
    denied = decide_diagnosis_access(
        policy=_RESTRICTED, tool_name="active_tcp_probe", arguments=args, node_roles=_ROLES
    )
    assert (denied.allowed, denied.reason) == (False, "unknown_target")


def test_every_scenario_selects_only_mountable_servers() -> None:
    from nika.mcp.registry import MCP_SERVER_SPECS, SUBMISSION_SERVER
    from nika.mcp.registry import select_diagnosis_servers
    from nika.net_env.net_env_pool import (
        list_all_net_envs,
        scenario_supported_backends,
    )

    for scenario in list_all_net_envs():
        for backend in scenario_supported_backends(scenario):
            servers = select_diagnosis_servers(scenario, backend=backend)
            assert len(servers) == len(set(servers)), scenario
            assert SUBMISSION_SERVER not in servers
            specs = [MCP_SERVER_SPECS[name] for name in servers]
            assert all(s.backend in {None, backend} for s in specs), scenario
            assert sum(s.role == "routing" for s in specs) <= 1, scenario
