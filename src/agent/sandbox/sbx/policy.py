"""Sandbox network policy helpers for MCP gateway and LLM API access."""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
from urllib.parse import urlparse

from agent.sandbox.config import SANDBOX_GATEWAY_HOST_BRIDGE
from agent.sandbox.sbx.client import (
    SBX_BIN,
    ensure_sbx_ready,
    run_sbx_checked,
    run_sbx_optional,
)

logger = logging.getLogger(__name__)

_MCP_POLICY_PREFIX = "nika-mcp-"

# The default balanced policy blocks model APIs.  These are the provider
# endpoints supported by NIKA's sandbox agent configurations, including an
# Anthropic-compatible OpenRouter endpoint used by some Claude credentials.
_LLM_NETWORK_HOSTS = (
    "api.anthropic.com",
    "api.deepseek.com",
    "openrouter.ai",
)

# Needed when SDK agents install deps from PyPI (offline wheels disabled).
_PYPI_NETWORK_HOSTS = (
    "pypi.org",
    "files.pythonhosted.org",
    "pypi.python.org",
)


def mcp_policy_resource(port: int, *, host: str = "localhost") -> str:
    """Return an sbx network policy resource for an MCP gateway endpoint."""
    return f"{host}:{int(port)}"


def mcp_policy_resource_from_url(url: str, *, fallback_port: int | None = None) -> str:
    """Build ``host:port`` from an MCP gateway URL (local or remote)."""
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    # Docker Sandboxes exposes the host bridge through a localhost policy
    # resource even though clients address it as host.docker.internal.
    if host == SANDBOX_GATEWAY_HOST_BRIDGE:
        host = "localhost"
    if parsed.port is not None:
        port = parsed.port
    elif fallback_port is not None:
        port = fallback_port
    elif parsed.scheme == "https":
        port = 443
    else:
        port = 80
    return mcp_policy_resource(port, host=host)


def llm_network_resources_for_url(url: str) -> list[str]:
    """Return sbx ``policy allow network`` resources for an LLM base URL.

    Remote hosts are allowed by hostname. Host-bridge URLs
    (``host.docker.internal`` / localhost) are reached as ``localhost:port``
    inside the microVM and need an explicit host:port allow rule.
    """
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return []
    if host in {SANDBOX_GATEWAY_HOST_BRIDGE, "localhost", "127.0.0.1"}:
        if parsed.port is not None:
            port = parsed.port
        elif parsed.scheme == "https":
            port = 443
        else:
            port = 80
        return [mcp_policy_resource(port, host="localhost")]
    return [host]


def ensure_llm_network_policy(*extra_hosts: str) -> None:
    """Allow outbound LLM API hosts needed for NIKA sandbox agents.

    *extra_hosts* covers custom / self-hosted endpoints (hostnames or
    ``host:port`` resources from :func:`llm_network_resources_for_url`).
    """
    ensure_sbx_ready()
    seen: set[str] = set()
    hosts: list[str] = []
    for host in (*_LLM_NETWORK_HOSTS, *extra_hosts):
        cleaned = (host or "").strip().lower()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        hosts.append(cleaned)
    for host in hosts:
        proc = run_sbx_optional(["policy", "allow", "network", host])
        if proc.returncode != 0:
            combined = f"{proc.stdout}\n{proc.stderr}".lower()
            if "already" in combined:
                continue
            logger.warning(
                "Failed to allow sbx network host %s: %s",
                host,
                (proc.stderr or proc.stdout).strip(),
            )


def ensure_pypi_network_policy() -> None:
    """Allow PyPI hosts for in-sandbox SDK package installs."""
    ensure_sbx_ready()
    for host in _PYPI_NETWORK_HOSTS:
        proc = run_sbx_optional(["policy", "allow", "network", host])
        if proc.returncode != 0:
            combined = f"{proc.stdout}\n{proc.stderr}".lower()
            if "already" in combined:
                continue
            logger.warning(
                "Failed to allow sbx network host %s: %s",
                host,
                (proc.stderr or proc.stdout).strip(),
            )


def allow_mcp_gateway(
    *,
    sandbox_name: str,
    port: int,
    host: str = "localhost",
    gateway_url: str | None = None,
) -> str:
    """Allow a sandbox to reach the MCP gateway.

    Prefer *gateway_url* when set (remote lab host); otherwise ``{host}:{port}``.
    """
    ensure_sbx_ready()
    if gateway_url:
        resource = mcp_policy_resource_from_url(gateway_url, fallback_port=port)
    else:
        resource = mcp_policy_resource(port, host=host)
    run_sbx_checked(
        [
            "policy",
            "allow",
            "network",
            "--sandbox",
            sandbox_name,
            resource,
        ]
    )
    return resource


def deny_mcp_gateway(
    *,
    sandbox_name: str,
    port: int,
    host: str = "localhost",
    gateway_url: str | None = None,
) -> None:
    """Revoke MCP gateway access for a sandbox."""
    if gateway_url:
        resource = mcp_policy_resource_from_url(gateway_url, fallback_port=port)
    else:
        resource = mcp_policy_resource(port, host=host)
    # ``sbx policy deny`` can hang against a stopped/missing sandbox; bound it.
    try:
        subprocess.run(
            [
                SBX_BIN,
                "policy",
                "deny",
                "network",
                "--sandbox",
                sandbox_name,
                resource,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "Timed out denying MCP gateway policy for %s (%s)",
            sandbox_name,
            resource,
        )


# Linux HOST_NAME_MAX is typically 64 (including NUL); sethostname rejects longer.
_SBX_HOSTNAME_MAX = 63


def sanitize_sandbox_name(session_id: str) -> str:
    """Return an sbx-compatible sandbox name for *session_id*.

    Names must fit the Linux hostname limit used by Docker Sandboxes
    (``sethostname``), while remaining unique for long benchmark trial IDs.
    """
    cleaned = re.sub(r"[^a-zA-Z0-9.\-+]", "-", session_id.strip())
    cleaned = cleaned.strip(".-+") or "session"
    prefix = "nika-"
    if len(prefix) + len(cleaned) <= _SBX_HOSTNAME_MAX:
        return f"{prefix}{cleaned}"
    digest = hashlib.sha1(cleaned.encode()).hexdigest()[:8]
    # nika-{head}-{digest}
    keep = _SBX_HOSTNAME_MAX - len(prefix) - 1 - len(digest)
    head = cleaned[: max(keep, 1)].rstrip(".-+")
    return f"{prefix}{head}-{digest}"
