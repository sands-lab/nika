"""Local safe_tool so the in-node bundle need not import nika.utils."""

from __future__ import annotations

import asyncio
from functools import wraps
from typing import Any, Callable


def safe_tool(func: Callable) -> Callable:
    """Convert tool exceptions into structured MCP-safe error payloads."""

    if asyncio.iscoroutinefunction(func):

        @wraps(func)
        async def async_wrapper(*args, **kwargs) -> Any:
            try:
                return await func(*args, **kwargs)
            except Exception as exc:
                return {"error": "tool_execution_error", "details": str(exc)}

        return async_wrapper

    @wraps(func)
    def sync_wrapper(*args, **kwargs) -> Any:
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            return {"error": "tool_execution_error", "details": str(exc)}

    return sync_wrapper
