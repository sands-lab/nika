"""Load mcp-agent config once without Pydantic 2.12 deprecation noise."""

from __future__ import annotations

import warnings

import pydantic

with warnings.catch_warnings():
    warnings.simplefilter("ignore", pydantic.PydanticDeprecatedSince212)
    import mcp_agent.config  # noqa: F401
