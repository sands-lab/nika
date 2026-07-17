from __future__ import annotations

import pytest
import os
from unittest.mock import patch
from agent.sandbox.config import ENV_AGENT_SANDBOX, resolve_sandbox_config
from agent.sandbox.env import build_sandbox_env, format_env_for_log
from agent.sandbox.redact import redact_env_value, redact_text
from nika.service.mcp_gateway.app import create_gateway_app
from nika.service.mcp_gateway.session_registry import clear_sessions, register_session
from starlette.testclient import TestClient


class SandboxConfigTest:
    def test_disabled_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = resolve_sandbox_config()
        assert not config.enabled

    def test_enabled_from_env(self) -> None:
        with patch.dict(os.environ, {ENV_AGENT_SANDBOX: "true"}, clear=False):
            config = resolve_sandbox_config()
        assert config.enabled

    def test_host_network_gateway_host(self) -> None:
        from agent.sandbox.config import (
            SANDBOX_GATEWAY_HOST_BRIDGE,
            SANDBOX_GATEWAY_HOST_HOSTNET,
            sandbox_gateway_agent_host,
        )

        assert sandbox_gateway_agent_host("host") == SANDBOX_GATEWAY_HOST_HOSTNET
        assert sandbox_gateway_agent_host("bridge") == SANDBOX_GATEWAY_HOST_BRIDGE

    def test_auto_proxy_requires_local_opt_in(self) -> None:
        from agent.sandbox.config import resolve_sandbox_proxy
        from pathlib import Path

        with patch.dict(
            os.environ, {"HTTP_PROXY": "http://127.0.0.1:7890"}, clear=False
        ):
            with patch(
                "agent.sandbox.config._clash_proxy_reachable", return_value=True
            ):
                http, https, _ = resolve_sandbox_proxy(
                    network="host",
                    env_file=Path("/nonexistent"),
                    local_env_file=Path("/nonexistent"),
                )
        assert http is None
        assert https is None

    def test_explicit_sandbox_proxy_vars(self) -> None:
        from agent.sandbox.config import resolve_sandbox_proxy
        from pathlib import Path
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write("NIKA_SANDBOX_HTTP_PROXY=http://proxy.test:8080\n")
            fh.write("NIKA_SANDBOX_HTTPS_PROXY=http://proxy.test:8080\n")
            local_path = Path(fh.name)
        try:
            http, https, no_proxy = resolve_sandbox_proxy(
                network="bridge",
                env_file=Path("/nonexistent"),
                local_env_file=local_path,
            )
            assert http == "http://proxy.test:8080"
            assert https == "http://proxy.test:8080"
            assert "127.0.0.1" in (no_proxy or "")
        finally:
            local_path.unlink(missing_ok=True)

    def test_auto_proxy_on_host_network_when_enabled(self) -> None:
        from agent.sandbox.config import DEFAULT_CLASH_HTTP_PROXY, resolve_sandbox_proxy
        from pathlib import Path
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write("NIKA_SANDBOX_AUTO_PROXY=true\n")
            local_path = Path(fh.name)
        try:
            with patch(
                "agent.sandbox.config._clash_proxy_reachable", return_value=True
            ):
                http, https, no_proxy = resolve_sandbox_proxy(
                    network="host",
                    env_file=Path("/nonexistent"),
                    local_env_file=local_path,
                )
            assert http == DEFAULT_CLASH_HTTP_PROXY
            assert https == DEFAULT_CLASH_HTTP_PROXY
            assert "127.0.0.1" in (no_proxy or "")
        finally:
            local_path.unlink(missing_ok=True)


class SandboxRedactionTest:
    def test_redacts_api_keys(self) -> None:
        assert redact_env_value("OPENAI_API_KEY", "sk-test-secret") == "***REDACTED***"

    def test_redacts_command_text(self) -> None:
        text = "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz"
        assert "REDACTED" in redact_text(text)
        assert "sk-abcdefghijklmnopqrstuvwxyz" not in redact_text(text)

    def test_env_log_has_no_raw_secrets(self) -> None:
        env = format_env_for_log(
            {"OPENAI_API_KEY": "sk-test-secret", "NIKA_AGENT_TYPE": "mock"}
        )
        assert env["OPENAI_API_KEY"] == "***REDACTED***"
        assert env["NIKA_AGENT_TYPE"] == "mock"


class GatewayPhaseRouteTest:
    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        clear_sessions()
        yield
        clear_sessions()

    def test_advance_phase_via_http(self) -> None:
        register_session("sess-1", scenario_name="simple_bgp")
        client = TestClient(create_gateway_app())
        response = client.post(
            "/gateway/sessions/sess-1/phase",
            headers={"NIKA-Session-Id": "sess-1"},
            json={"phase": "submission"},
        )
        assert response.status_code == 200
        assert response.json()["phase"] == "submission"

    def test_health_endpoint(self) -> None:
        client = TestClient(create_gateway_app())
        response = client.get("/gateway/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class SandboxEnvBuildTest:
    def test_builds_whitelisted_env(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "sk-test",
                "NIKA_MAX_STEPS": "7",
                "NIKA_SESSION_BACKEND": "kathara",
            },
            clear=False,
        ):
            env = build_sandbox_env(
                session_id="sess-1",
                session_dir="/results/sess-1",
                agent_type="local_cli.codex_cli",
                model="gpt-5.4-mini",
                max_steps=7,
                reasoning_effort=None,
                llm_provider=None,
                mcp_gateway_agent_url="http://host.docker.internal:12345",
                env_file=__import__("pathlib").Path("/nonexistent"),
                skills_dir="/nika/skills",
            )
        assert env["NIKA_SANDBOX_EXECUTION"] == "1"
        assert env["NIKA_MCP_GATEWAY_AGENT_URL"] == "http://host.docker.internal:12345"
        assert env["OPENAI_API_KEY"] == "sk-test"
        assert env["NIKA_SESSION_BACKEND"] == "kathara"


class SandboxMcpRegistryTest:
    def test_select_diagnosis_servers_without_kathara_import(self) -> None:
        with patch.dict(
            os.environ,
            {"NIKA_SANDBOX_EXECUTION": "1", "NIKA_SESSION_BACKEND": "kathara"},
            clear=False,
        ):
            from nika.service.mcp_server.registry import select_diagnosis_servers

            servers = select_diagnosis_servers("simple_bgp")
        assert "kathara_base_mcp_server" in servers
        assert "kathara_frr_mcp_server" in servers


class SandboxImageTest:
    def test_ensure_skips_when_image_exists(self) -> None:
        from agent.sandbox.image import ensure_sandbox_image

        with patch(
            "agent.sandbox.image.sandbox_image_exists", return_value=True
        ) as exists:
            with patch("agent.sandbox.image.build_sandbox_image") as build:
                ensure_sandbox_image("nika/agent-sandbox:latest")
        exists.assert_called_once_with("nika/agent-sandbox:latest")
        build.assert_not_called()

    def test_ensure_builds_when_image_missing(self) -> None:
        from agent.sandbox.image import ensure_sandbox_image

        with patch(
            "agent.sandbox.image.sandbox_image_exists", side_effect=[False, True]
        ):
            with patch("agent.sandbox.image.build_sandbox_image") as build:
                ensure_sandbox_image(
                    "nika/agent-sandbox:latest", http_proxy="http://127.0.0.1:7890"
                )
        build.assert_called_once_with(
            "nika/agent-sandbox:latest",
            http_proxy="http://127.0.0.1:7890",
            https_proxy=None,
        )
