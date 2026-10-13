"""Isolation of e2b-backend sandboxes against a real E2B-compatible service.

Runs when ``nika.sandbox.url`` and ``E2B_API_KEY`` are configured.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent.sandbox.config import SandboxConfig, resolve_sandbox_config

pytestmark = [pytest.mark.integration, pytest.mark.sandbox]


def _e2b_config() -> SandboxConfig | None:
    try:
        import e2b  # noqa: F401
    except ImportError:
        return None
    from agent.sandbox.e2b.backend import _api_key

    config = replace(resolve_sandbox_config(), backend="e2b", keep_container=False)
    if not config.url or not _api_key(config.env_file):
        return None
    return config


class _Ok(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


@pytest.mark.skipif(
    _e2b_config() is None,
    reason="e2b service not configured (nika.sandbox.url, E2B_API_KEY)",
)
def test_e2b_sandbox_reaches_only_its_tunnel(tmp_path) -> None:
    """The sandbox reaches its mapped host service, no other host port, no internet."""
    from agent.sandbox.e2b.backend import E2bBackend

    backend = E2bBackend(_e2b_config())
    backend._template = backend.ensure_template("shell")
    servers = [ThreadingHTTPServer(("127.0.0.1", 0), _Ok) for _ in range(2)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    own_port, peer_port = (server.server_address[1] for server in servers)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    backend.stage_workspace(workspace, agent_type="cli.claude")

    sandbox_id = backend.create(
        agent_session_id=f"e2b-isolation-{uuid.uuid4().hex[:6]}",
        agent_type="cli.claude",
        workspace_dir=workspace,
    )
    try:
        backend.allow_gateway(
            sandbox_id, port=own_port, gateway_url=f"http://127.0.0.1:{own_port}"
        )
        probe = " ; echo ; ".join(
            f"curl -s -o /dev/null -w '%{{http_code}}' -m 5 {url} || true"
            for url in (
                f"http://127.0.0.1:{own_port}/",
                f"http://127.0.0.1:{peer_port}/",
                "https://pypi.org/simple/",
            )
        )
        proc = backend.exec_stream(sandbox_id, probe)
        output = "".join(proc.stdout)
        proc.wait()
        own, peer, internet = output.split()
        assert own == "200", output
        assert peer == "000", output
        assert internet == "000", output
    finally:
        backend.revoke_gateway(
            sandbox_id, port=own_port, gateway_url=f"http://127.0.0.1:{own_port}"
        )
        backend.remove(sandbox_id)
        backend.release_host()
        for server in servers:
            server.shutdown()
            server.server_close()
