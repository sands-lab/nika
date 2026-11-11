from concurrent.futures import ThreadPoolExecutor

import pytest

from nika.mcp.gateway.session_registry import (
    advance_phase,
    clear_sessions,
    register_session,
    reserve_tool_call,
)
from nika.run_config.schema import AgentSettings

pytestmark = pytest.mark.unit


def test_primitive_budget_is_shared_across_aliases_and_concurrent_connections():
    clear_sessions()
    register_session(
        "canonical",
        agent_session_id="opaque",
        tool_call_limits={"diagnosis": 7, "submission": 1},
    )
    with ThreadPoolExecutor(8) as pool:
        accepted = list(
            pool.map(
                lambda i: reserve_tool_call("opaque" if i % 2 else "canonical")[0],
                range(30),
            )
        )
    assert sum(accepted) == 7
    advance_phase("opaque", "submission")
    assert reserve_tool_call("canonical")[0]
    assert not reserve_tool_call("opaque")[0]
    clear_sessions()


def test_negative_budget_is_rejected_and_defaults_remain_unlimited():
    with pytest.raises(ValueError):
        AgentSettings(tool_call_limits={"diagnosis": -1})
    assert AgentSettings().tool_call_limits == {}
