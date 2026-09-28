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
