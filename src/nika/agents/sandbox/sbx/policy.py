"""Sandbox network policy helpers for MCP gateway and LLM API access."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from collections.abc import Iterable, Sequence
from fnmatch import fnmatchcase
from typing import Any
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
# Where ``uv venv --python`` downloads a managed interpreter.
_PYTHON_DOWNLOAD_HOSTS = (
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
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


def _clean_hosts(hosts: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(h.strip().lower() for h in hosts if (h or "").strip()))


def allow_network_hosts(sandbox_name: str, *hosts: str) -> None:
    """Allow *sandbox_name* outbound traffic to *hosts* (agent egress, PyPI)."""
    for host in _clean_hosts(hosts):
        run_sbx_checked(["policy", "allow", "network", "--sandbox", sandbox_name, host])


def revoke_network_hosts(sandbox_name: str, *hosts: str) -> None:
    """Remove per-sandbox allow rules added by :func:`allow_network_hosts`."""
    for host in _clean_hosts(hosts):
        run_sbx_optional(
            [
                "policy",
                "rm",
                "network",
                "--force",
                "--sandbox",
                sandbox_name,
                "--resource",
                host,
            ]
        )


def ensure_pypi_network_policy(sandbox_name: str) -> None:
    """Allow PyPI hosts in *sandbox_name* for in-sandbox SDK package installs."""
    allow_network_hosts(sandbox_name, *_PYPI_NETWORK_HOSTS)


def revoke_pypi_network_policy(sandbox_name: str) -> None:
    revoke_network_hosts(sandbox_name, *_PYPI_NETWORK_HOSTS)


def _resource_host(resource: str) -> str:
    host, sep, port = resource.strip().lower().rpartition(":")
    return host if sep and port.isdigit() else resource.strip().lower()


def resource_matches_host(resource: str, hosts: Iterable[str]) -> bool:
    """True when sbx network *resource* (``**.``/``*`` patterns, port) covers a host."""
    pattern = _resource_host(resource).replace("**", "*")
    return any(fnmatchcase(_resource_host(host), pattern) for host in hosts)


def allowed_network_resources(policy: Any, sandbox_name: str) -> list[str]:
    """Network resources allowed to *sandbox_name* by ``sbx policy ls --json``.

    Covers global rules (``applies_to == "all"``) and rules sbx provisioned for
    this sandbox, such as the template's model API hosts.
    """
    scopes = {"all", f"sandbox:{sandbox_name}"}
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if (
                node.get("resource_type") == "network"
                and node.get("decision") == "allow"
                and node.get("applies_to") in scopes
            ):
                found.extend(str(r) for r in node.get("resources") or ())
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(policy)
    return list(dict.fromkeys(r.strip() for r in found if r.strip()))


def sandbox_network_lockdown(
    sandbox_name: str,
    *,
    keep_hosts: Sequence[str] = (),
    keep_resources: Sequence[str] = (),
    install_pypi: bool = False,
    install_python: bool = False,
) -> tuple[list[str], list[str]]:
    """Return ``(rest, install)`` resources to deny for *sandbox_name*.

    Resources covering *keep_hosts* (an added agent's egress) or equal to
    *keep_resources* stay allowed. With *install_pypi*, resources covering the
    package index hosts form the second list, denied once installs finish;
    with *install_python*, so do those covering uv's interpreter downloads.
    """
    proc = run_sbx_checked(["policy", "ls", "--json"], timeout=120)
    resources = [
        r
        for r in allowed_network_resources(json.loads(proc.stdout), sandbox_name)
        if r not in keep_resources and not resource_matches_host(r, keep_hosts)
    ]
    install_hosts = (_PYPI_NETWORK_HOSTS if install_pypi else ()) + (
        _PYTHON_DOWNLOAD_HOSTS if install_python else ()
    )
    install = [r for r in resources if resource_matches_host(r, install_hosts)]
    rest = [r for r in resources if r not in install]
    return rest, install


def deny_network_resources(sandbox_name: str, resources: Sequence[str]) -> None:
    """Deny *resources* for *sandbox_name*; deny wins over a global allow."""
    if resources:
        run_sbx_checked(
            [
                "policy",
                "deny",
                "network",
                "--sandbox",
                sandbox_name,
                ",".join(resources),
            ],
            timeout=600,
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
    # The lockdown may have denied this resource (a stale global allow for the
    # same port); a deny would win over the allow.
    revoke_network_hosts(sandbox_name, resource)
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
