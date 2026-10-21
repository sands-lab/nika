"""MCP client config of the task's session, for agents in their sandbox."""

from __future__ import annotations

import functools
from datetime import timedelta
from typing import Any

from agent.protocols import PHASES, SUBMISSION
from agent.runtime.contract import SUBMISSION_SERVER
from agent.runtime.task import current_task


def filter_phase_servers(servers: dict, phase: str) -> dict:
    """Keep only the MCP servers an agent may use in *phase*.

    The gateway enforces phase access too, but keeping the task server out of
    the diagnosis config also keeps the fault catalog out of the agent's tool
    inventory, and keeps diagnosis servers out of the submission config.
    """
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {PHASES}, got {phase!r}")
    if phase == SUBMISSION:
        return {name: cfg for name, cfg in servers.items() if name == SUBMISSION_SERVER}
    return {name: cfg for name, cfg in servers.items() if name != SUBMISSION_SERVER}


def session_mcp_config(phase: str | None = None) -> dict[str, dict[str, Any]]:
    """HTTP MCP client entries of the session, keyed by server name.

    With *phase*, only that phase's servers (see :func:`filter_phase_servers`).
    Entries carry ``session_kwargs`` with the MCP read timeout when the
    installed LangChain adapter accepts it.
    """
    servers = {name: dict(cfg) for name, cfg in current_task().mcp_servers.items()}
    if phase is not None:
        servers = filter_phase_servers(servers, phase)
    read_timeout = _mcp_read_timeout()
    if read_timeout is not None:
        for cfg in servers.values():
            # Forwarded to mcp.ClientSession(read_timeout_seconds=...):
            # a lost/blocked response raises McpError instead of hanging.
            cfg["session_kwargs"] = {"read_timeout_seconds": read_timeout}
    return servers


def mcp_read_timeout_seconds() -> float | None:
    """Return the MCP read timeout in seconds, or ``None`` when disabled.

    LangGraph, AutoGen, and mcp-agent share this value. A read timeout keeps a
    lost MCP response from blocking the whole run.
    """
    seconds = float(current_task().mcp_read_timeout_sec)
    return seconds if seconds > 0 else None


@functools.lru_cache(maxsize=1)
def _mcp_read_timeout() -> timedelta | None:
    seconds = mcp_read_timeout_seconds()
    if seconds is None or not _adapter_supports_session_kwargs():
        return None
    return timedelta(seconds=seconds)


def _adapter_supports_session_kwargs() -> bool:
    """Return whether the installed adapter accepts ``session_kwargs``."""
    for module_name in (
        "langchain_mcp_adapters.sessions",
        "langchain_mcp_adapters.client",
    ):
        try:
            module = __import__(
                module_name, fromlist=["StreamableHttpConnection", "StdioConnection"]
            )
        except ImportError:
            continue
        for class_name in ("StreamableHttpConnection", "StdioConnection"):
            connection = getattr(module, class_name, None)
            if connection is not None:
                return "session_kwargs" in getattr(connection, "__annotations__", {})
    return False


def _flatten_exception(exc: BaseException) -> str:
    """Readable one-line summary of *exc*, unwrapping ExceptionGroups."""
    if isinstance(exc, BaseExceptionGroup):
        parts = [_flatten_exception(sub) for sub in exc.exceptions]
        return "; ".join(dict.fromkeys(parts))
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _hardened_coroutine(tool_name: str, coro_fn):
    from langchain_core.tools import ToolException

    @functools.wraps(coro_fn)
    async def wrapped(*args, **kwargs):
        try:
            return await coro_fn(*args, **kwargs)
        except ToolException:
            raise
        except Exception as exc:  # noqa: BLE001 - includes ExceptionGroup
            raise ToolException(
                f"MCP tool '{tool_name}' failed with a transport error "
                f"({_flatten_exception(exc)}). Any result was lost; retry the "
                "call if you still need it."
            ) from exc

    return wrapped


def harden_mcp_tools(tools) -> None:
    """Convert transport-level failures of MCP tools into ToolExceptions.

    langchain opens a fresh MCP session per tool call, and the client's
    stream reader can hit a teardown race (e.g. BrokenResourceError inside an
    ExceptionGroup) when the server flushes output while the session closes.
    ``handle_tool_error = True`` only catches ToolException, so without this
    wrapper one such race escapes the tool node and kills the whole run. With
    it, the agent sees an error ToolMessage and can just retry.
    """
    for tool in tools:
        if getattr(tool, "coroutine", None) is not None:
            tool.coroutine = _hardened_coroutine(tool.name, tool.coroutine)
