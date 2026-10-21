"""Sandbox network policy helpers for MCP gateway and LLM API access."""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
from urllib.parse import urlparse

from nika.agents.sandbox.config import SANDBOX_GATEWAY_HOST_BRIDGE
from nika.agents.sandbox.sbx.client import (
    ensure_sbx_ready,
    run_sbx,
    run_sbx_checked,
    run_sbx_optional,
)

logger = logging.getLogger(__name__)

_MCP_POLICY_PREFIX = "nika-mcp-"

# Needed when agents install packages from PyPI (offline wheels disabled).
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


def allow_network_hosts(*hosts: str) -> None:
    """Allow outbound traffic to *hosts* (an added agent's declared egress)."""
    if not any((host or "").strip() for host in hosts):
        return
    ensure_sbx_ready()
    for host in dict.fromkeys(h.strip().lower() for h in hosts if h.strip()):
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
    # Remove the allow rule: sbx rejects a deny rule that conflicts with it.
    # ``sbx policy`` can hang against a stopped/missing sandbox; bound it.
    try:
        proc = run_sbx(
            [
                "policy",
                "rm",
                "network",
                "--force",
                "--sandbox",
                sandbox_name,
                "--resource",
                resource,
            ],
            check=False,
            capture_output=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "Timed out revoking MCP gateway policy for %s (%s)",
            sandbox_name,
            resource,
        )
        return
    if proc.returncode != 0:
        logger.warning(
            "Failed to revoke MCP gateway policy for %s (%s): %s",
            sandbox_name,
            resource,
            (proc.stderr or proc.stdout).strip(),
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
    # The name is the microVM hostname, so it must not name the host project.
    prefix = "agent-"
    if len(prefix) + len(cleaned) <= _SBX_HOSTNAME_MAX:
        return f"{prefix}{cleaned}"
    digest = hashlib.sha1(cleaned.encode()).hexdigest()[:8]
    # agent-{head}-{digest}
    keep = _SBX_HOSTNAME_MAX - len(prefix) - 1 - len(digest)
    head = cleaned[: max(keep, 1)].rstrip(".-+")
    return f"{prefix}{head}-{digest}"
