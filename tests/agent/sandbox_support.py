from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent.sandbox.config import sandbox_gateway_agent_host
from agent.sandbox.sbx.client import (
    ensure_sbx_ready,
    run_sbx_checked,
    run_sbx_optional,
    sbx_available,
    sbx_authenticated,
)
from agent.sandbox.sbx.policy import (
    allow_mcp_gateway,
    deny_mcp_gateway,
    sanitize_sandbox_name,
)
from agent.sandbox.sbx.workspace import prepare_workspace
from nika.mcp.gateway.lifecycle import (
    ENV_GATEWAY_AGENT_URL,
    mcp_gateway_for_session,
)

SECURITY_PROBE_SCRIPT = Path(__file__).resolve().parent / "security_probe.sh"


def docker_available() -> bool:
    return shutil.which("docker") is not None


def sandbox_runtime_available() -> bool:
    return sbx_available() and sbx_authenticated()


def sandbox_openai_credential_available() -> bool:
    from agent.sandbox.sbx.credentials import sbx_openai_credential_available
    from nika.config import _REPO_ROOT

    return sbx_openai_credential_available(env_file=_REPO_ROOT / ".env")


def sandbox_anthropic_credential_available() -> bool:
    from agent.sandbox.sbx.credentials import sbx_anthropic_credential_available
    from nika.config import _REPO_ROOT

    return sbx_anthropic_credential_available(env_file=_REPO_ROOT / ".env")


def run_security_probe_with_gateway(session_id: str = "sandbox-security-test") -> None:
    """Start a gateway and verify native sbx sandbox probes against it."""
    if not sandbox_runtime_available():
        raise RuntimeError("sbx is required for security probe")
    if not SECURITY_PROBE_SCRIPT.is_file():
        raise FileNotFoundError(f"Missing probe script: {SECURITY_PROBE_SCRIPT}")

    ensure_sbx_ready()

    sandbox_name = sanitize_sandbox_name(session_id)
    agent_host = sandbox_gateway_agent_host()
    agent_type = "cli.codex"
    sbx_agent = "shell"

    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        session_dir.mkdir()
        ground_truth = session_dir / "ground_truth.json"
        ground_truth.write_text('{"hidden": true}', encoding="utf-8")
        host_probe = Path(tmp) / "host_secret_probe.txt"
        host_probe.write_text("host-only", encoding="utf-8")

        manifest = {
            "session_id": session_id,
            "session_dir": str(session_dir),
            "agent_type": agent_type,
            "model": "gpt-5.4-mini",
            "task_description": "probe",
            "mcp_gateway_agent_url": "",
            "backend": "kathara",
        }
        workspace = prepare_workspace(
            session_dir=session_dir,
            manifest=manifest,
            runtime_env={"NIKA_AGENT_TYPE": agent_type},
        )
        probe_in_workspace = workspace.workspace_dir / "security_probe.sh"
        shutil.copy(SECURITY_PROBE_SCRIPT, probe_in_workspace)
        probe_in_workspace.chmod(0o755)

        with mcp_gateway_for_session(
            session_id,
            scenario_name="simple_bgp",
            sandbox=True,
            sandbox_agent_host=agent_host,
        ) as gateway_manager:
            gateway_url = os.environ[ENV_GATEWAY_AGENT_URL]
            manifest["mcp_gateway_agent_url"] = gateway_url
            workspace.manifest_path.write_text(
                json.dumps(manifest, indent=2),
                encoding="utf-8",
            )

            run_sbx_optional(["rm", "--force", sandbox_name])
            try:
                create_cmd = [
                    "create",
                    "--name",
                    sandbox_name,
                    sbx_agent,
                    str(workspace.workspace_dir),
                ]
                run_sbx_checked(create_cmd)
                allow_mcp_gateway(sandbox_name=sandbox_name, port=gateway_manager.port)

                env_prefix = (
                    f"NIKA_SBX_NATIVE=1 "
                    f"NIKA_MCP_GATEWAY_AGENT_URL={gateway_url} "
                    f"NIKA_SANDBOX_PROBE_FILE={host_probe} "
                    f"NIKA_SANDBOX_GROUND_TRUTH={ground_truth}"
                )
                result = subprocess.run(
                    [
                        "sbx",
                        "exec",
                        "-d",
                        sandbox_name,
                        "bash",
                        "-lc",
                        f"cd {workspace.workspace_dir} && {env_prefix} bash ./security_probe.sh",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.returncode != 0:
                    raise AssertionError(
                        f"Security probe failed (code {result.returncode}):\n"
                        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
                    )
            finally:
                deny_mcp_gateway(sandbox_name=sandbox_name, port=gateway_manager.port)
                run_sbx_optional(["rm", "--force", sandbox_name])


def run_cross_sandbox_isolation_probe() -> None:
    """Prove a sandbox can only reach its allowed MCP gateway port.

    Starts two lightweight host listeners on distinct ports, creates one sbx
    microVM, allows only the first port, then checks from inside the VM that
    the allowed port succeeds and the peer port is blocked by network policy.
    """
    if not sandbox_runtime_available():
        raise RuntimeError("sbx is required for cross-sandbox isolation probe")

    ensure_sbx_ready()
    agent_host = sandbox_gateway_agent_host()
    session_id = "sandbox-iso-probe"
    sandbox_name = sanitize_sandbox_name(session_id)

    class _HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

    servers: list[ThreadingHTTPServer] = []
    ports: list[int] = []
    try:
        for _ in range(2):
            server = ThreadingHTTPServer(("0.0.0.0", 0), _HealthHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            servers.append(server)
            ports.append(int(server.server_address[1]))
        own_port, peer_port = ports
        assert own_port != peer_port

        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp) / session_id
            session_dir.mkdir()
            workspace = prepare_workspace(
                session_dir=session_dir,
                manifest={
                    "session_id": session_id,
                    "session_dir": str(session_dir),
                    "agent_type": "cli.codex",
                    "model": "gpt-5.4-mini",
                    "task_description": "isolation-probe",
                    "mcp_gateway_agent_url": f"http://{agent_host}:{own_port}",
                    "backend": "kathara",
                },
                runtime_env={"NIKA_AGENT_TYPE": "cli.codex"},
            )

            run_sbx_optional(["rm", "--force", sandbox_name])
            try:
                run_sbx_checked(
                    [
                        "create",
                        "--name",
                        sandbox_name,
                        "shell",
                        str(workspace.workspace_dir),
                    ]
                )
                allow_mcp_gateway(sandbox_name=sandbox_name, port=own_port)

                own_url = f"http://{agent_host}:{own_port}/"
                peer_url = f"http://{agent_host}:{peer_port}/"
                inner = (
                    "set -e; "
                    f"curl -sf --connect-timeout 3 --max-time 5 '{own_url}' "
                    f">/dev/null; "
                    f"peer_code=$(curl -s -o /tmp/nika_peer_body "
                    f"--connect-timeout 3 --max-time 5 "
                    f"-w '%{{http_code}}' '{peer_url}' || true); "
                    "peer_body=$(cat /tmp/nika_peer_body 2>/dev/null || true); "
                    'echo "peer_http=$peer_code"; '
                    'echo "peer_body=$peer_body"; '
                    'test "$peer_code" != "200"; '
                    'echo "$peer_body" | grep -qi "Blocked by network policy"'
                )
                result = subprocess.run(
                    ["sbx", "exec", "-d", sandbox_name, "bash", "-lc", inner],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=90,
                )
                if result.returncode != 0:
                    raise AssertionError(
                        f"MCP gateway port isolation failed for {sandbox_name} "
                        f"(own={own_port} peer={peer_port}):\n"
                        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
                    )
            finally:
                # Removing the sandbox drops its network policy; avoid a separate
                # deny which can hang against a stopped/partial sandbox.
                run_sbx_optional(["rm", "--force", sandbox_name])
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
