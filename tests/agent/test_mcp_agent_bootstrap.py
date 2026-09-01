"""Regression tests for byo.mcp_agent import hygiene."""

from __future__ import annotations

import importlib
import sys
import warnings

import pydantic


def test_mcp_agent_import_avoids_after_model_validator_deprecation() -> None:
    for name in list(sys.modules):
        if name == "agent.byo.mcp_agent._bootstrap" or name.startswith("mcp_agent"):
            del sys.modules[name]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", pydantic.PydanticDeprecatedSince212)
        importlib.import_module("agent.byo.mcp_agent._bootstrap")

    assert not any(
        issubclass(item.category, pydantic.PydanticDeprecatedSince212)
        for item in caught
    )
