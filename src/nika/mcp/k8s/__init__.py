"""Host-side Kubernetes MCP server for NIKA k8s labs.

Talks to the lab API server via the session kubeconfig written by post_deploy
(``localhost:<k8s_controller_port>``). Mounted in-process by the MCP gateway.
"""

from __future__ import annotations

from nika.mcp.k8s.server import SERVER_NAME, mcp

__all__ = ["SERVER_NAME", "mcp"]
