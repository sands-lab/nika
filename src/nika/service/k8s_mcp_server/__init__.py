"""In-node Kubernetes MCP server for NIKA k8s labs.

Runs on the Kathara controller with a local kubeconfig. Host-side NIKA only
routes agents to this process via the MCP gateway reverse proxy.
"""

from __future__ import annotations

__all__ = ["DEFAULT_PORT", "DEFAULT_BIND", "create_app"]

DEFAULT_PORT = 18765
DEFAULT_BIND = "0.0.0.0"


def create_app():
    from nika.service.k8s_mcp_server.server import create_app as _create_app

    return _create_app()
