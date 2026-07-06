"""Build HTTP MCP client configs for NIKA troubleshooting agents."""

from __future__ import annotations

import functools
import os
from datetime import timedelta

from langchain_core.tools import ToolException

from nika.service.mcp_gateway.lifecycle import ENV_GATEWAY_AGENT_URL, ENV_GATEWAY_URL
from nika.service.mcp_server.registry import (
    MCP_SERVER_SPECS,
    SUBMISSION_SERVER,
    select_diagnosis_servers,
)

__all__ = [
    "MCPServerConfig",
    "harden_mcp_tools",
    "select_diagnosis_servers",
    "select_session_servers",
    "session_http_headers",
]

SESSION_HEADER = "NIKA-Session-Id"


def session_http_headers(session_id: str) -> dict[str, str]:
    return {SESSION_HEADER: session_id}


def _gateway_base_url() -> str:
    if os.environ.get("NIKA_SANDBOX_EXECUTION") == "1":
        agent_base = os.environ.get(ENV_GATEWAY_AGENT_URL, "").strip().rstrip("/")
        if agent_base:
            return agent_base
    base = os.environ.get(ENV_GATEWAY_URL, "").strip().rstrip("/")
    if not base:
        raise RuntimeError(
            f"{ENV_GATEWAY_URL} is not set. Start the MCP gateway before building HTTP config."
        )
    return base


def select_session_servers(
    scenario_name: str,
    *,
    backend: str | None = None,
) -> list[str]:
    """Return all MCP server names for a troubleshooting session."""
    servers = select_diagnosis_servers(
        scenario_name,
        backend=backend,
    )
    if SUBMISSION_SERVER not in servers:
        servers.append(SUBMISSION_SERVER)
    return servers


# Read timeout for MCP client requests (seconds; 0 disables).
# Without it the mcp ClientSession waits FOREVER on a lost response — the
# observed failure mode: a benchmark run frozen for days with the server's
# "Processing request of type ListToolsRequest" as the last log line.
MCP_READ_TIMEOUT_ENV = "NIKA_MCP_READ_TIMEOUT"
DEFAULT_MCP_READ_TIMEOUT_SECONDS = 120.0


@functools.lru_cache(maxsize=1)
def _mcp_read_timeout() -> timedelta | None:
    raw = os.getenv(MCP_READ_TIMEOUT_ENV, "").strip()
    try:
        seconds = float(raw) if raw else DEFAULT_MCP_READ_TIMEOUT_SECONDS
    except ValueError:
        seconds = DEFAULT_MCP_READ_TIMEOUT_SECONDS
    if seconds <= 0:
        return None
    if not _adapter_supports_session_kwargs():
        print(
            "WARNING: installed langchain_mcp_adapters does not support "
            "session_kwargs — MCP calls have NO read timeout (hang risk); "
            "upgrade with `pip install -U langchain-mcp-adapters`."
        )
        return None
    return timedelta(seconds=seconds)


def _adapter_supports_session_kwargs() -> bool:
    """Feature-detect session_kwargs so an older adapter lib does not choke
    on an unknown connection key."""
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
    wrapper one such race escapes the tool node and kills the whole benchmark
    case. With it, the agent sees an error ToolMessage and can just retry.
    """
    for tool in tools:
        if getattr(tool, "coroutine", None) is not None:
            tool.coroutine = _hardened_coroutine(tool.name, tool.coroutine)


class MCPServerConfig:
    def __init__(self, session_id: str):
        if not session_id:
            raise ValueError("session_id is required to start MCP servers.")
        self.session_id = session_id

    def _build_http_entry(self, name: str) -> dict:
        if name not in MCP_SERVER_SPECS:
            raise KeyError(f"Unknown MCP server: {name!r}")
        base = _gateway_base_url()
        entry = {
            "transport": "http",
            "url": f"{base}/mcp/{name}/mcp",
            "headers": session_http_headers(self.session_id),
        }
        read_timeout = _mcp_read_timeout()
        if read_timeout is not None:
            # Forwarded to mcp.ClientSession(read_timeout_seconds=...):
            # a lost/blocked response raises McpError instead of hanging.
            entry["session_kwargs"] = {"read_timeout_seconds": read_timeout}
        return entry

    def load_http_config(self, server_names: list[str]) -> dict:
        """Return HTTP MCP client config for *server_names*."""
        return {
            name: self._build_http_entry(name)
            for name in server_names
            if name in MCP_SERVER_SPECS
        }

    def load_session_http_config(
        self,
        scenario_name: str,
        *,
        backend: str | None = None,
    ) -> dict:
        """Return HTTP MCP config for all servers needed by the session."""
        server_names = select_session_servers(
            scenario_name,
            backend=backend,
        )
        return self.load_http_config(server_names)

    # Backward-compatible aliases used in tests and docs during migration.
    def load_config(self, if_submit: bool = False) -> dict:
        if if_submit:
            return self.load_http_config([SUBMISSION_SERVER])
        names = [n for n, spec in MCP_SERVER_SPECS.items() if spec.role != "task"]
        return self.load_http_config(names)

    def load_filtered_config(self, server_names: list[str]) -> dict:
        return self.load_http_config(server_names)
