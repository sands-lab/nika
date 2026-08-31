"""Unit tests for NIKA Remote config, artifacts, API, and workflow branching."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from nika.remote.api import create_remote_app
from nika.remote.artifacts import pack_session_dir, unpack_session_dir
from nika.remote.client import RemoteClient
from nika.remote.config import (
    ENV_REMOTE_SERVER,
    RemoteConfig,
    is_remote_enabled,
)
from nika.remote.handlers import handle_env_start
from nika.remote.protocol import EnvStartRequest, EnvStartResponse
from nika.run_config.loader import reset_run_config, set_run_config
from nika.run_config.schema import RunConfig


@pytest.fixture(autouse=True)
def _clear_run_config(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_run_config()
    monkeypatch.delenv(ENV_REMOTE_SERVER, raising=False)
    yield
    reset_run_config()


def _enable_remote(url: str = "http://lab.example:8700") -> None:
    set_run_config(
        RunConfig.model_validate({"nika": {"remote": {"enabled": True, "url": url}}})
    )


def test_is_remote_enabled_requires_url() -> None:
    set_run_config(
        RunConfig.model_validate({"nika": {"remote": {"enabled": True, "url": None}}})
    )
    assert is_remote_enabled() is False
    _enable_remote()
    assert is_remote_enabled() is True


def test_is_remote_enabled_false_on_server(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_remote()
    monkeypatch.setenv(ENV_REMOTE_SERVER, "1")
    assert is_remote_enabled() is False


def test_gateway_url_uses_remote_host() -> None:
    cfg = RemoteConfig(
        enabled=True,
        url="http://10.0.0.5:8700",
        artifact_root="",
    )
    assert cfg.gateway_url(9123) == "http://10.0.0.5:9123"


def test_pack_unpack_session_dir(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "run.json").write_text('{"session_id":"s1"}', encoding="utf-8")
    nested = src / "nested"
    nested.mkdir()
    (nested / "gt.json").write_text("{}", encoding="utf-8")

    blob = pack_session_dir(src)
    dest = tmp_path / "dest"
    unpack_session_dir(blob, dest)
    assert (dest / "run.json").read_text(encoding="utf-8") == '{"session_id":"s1"}'
    assert (dest / "nested" / "gt.json").is_file()


def test_handle_env_start_logs(caplog: pytest.LogCaptureFixture) -> None:
    with (
        caplog.at_level(logging.INFO, logger="nika.remote"),
        patch(
            "nika.remote.handlers.start_net_env",
            return_value="sess-log",
        ),
        patch(
            "nika.remote.handlers.session_public_dict",
            return_value={"session_id": "sess-log"},
        ),
    ):
        handle_env_start(EnvStartRequest(scenario="simple_bgp"))
    messages = [r.getMessage() for r in caplog.records]
    assert any("env start begin" in m and "simple_bgp" in m for m in messages)
    assert any("env start done" in m and "sess-log" in m for m in messages)


def test_handle_env_start_forwards_static_validation() -> None:
    with (
        patch(
            "nika.remote.handlers.start_net_env", return_value="sess-static"
        ) as start,
        patch(
            "nika.remote.handlers.session_public_dict",
            return_value={"session_id": "sess-static"},
        ),
    ):
        handle_env_start(EnvStartRequest(scenario="isp_abilene", static_validation=True))
    assert start.call_args.kwargs["static_validation"] is True


def test_remote_app_health_no_auth() -> None:
    app = create_remote_app()
    client = TestClient(app)
    assert client.get("/health").json()["status"] == "ok"
    with patch(
        "nika.remote.api.handle_env_start",
        return_value=EnvStartResponse(
            session_id="sess-1",
            session={"session_id": "sess-1", "scenario_name": "x"},
        ),
    ):
        resp = client.post("/v1/env/start", json={"scenario": "x"})
    assert resp.status_code == 200
    assert resp.json()["session_id"] == "sess-1"


def test_remote_app_artifacts(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "ground_truth.json").write_text("{}", encoding="utf-8")
    app = create_remote_app()
    client = TestClient(app)
    with patch(
        "nika.remote.api.handle_artifacts",
        return_value=pack_session_dir(session_dir),
    ):
        resp = client.get("/v1/sessions/sess-1/artifacts")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/gzip")
    out = tmp_path / "out"
    unpack_session_dir(resp.content, out)
    assert (out / "ground_truth.json").is_file()


def test_remote_client_rewrites_gateway_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = RemoteConfig(
        enabled=True,
        url="http://lab.example:8700",
        artifact_root="",
    )
    client = RemoteClient(cfg)

    def fake_request(method, path, **kwargs):
        assert method == "POST"
        assert path.endswith("/mcp/attach")
        return {
            "session_id": "s1",
            "gateway_port": 9555,
            "gateway_base_url": "http://0.0.0.0:9555",
        }

    monkeypatch.setattr(client, "_request", fake_request)
    attach = client.mcp_attach("s1", public_host="lab.example")
    assert attach.gateway_base_url == "http://lab.example:9555"
    assert attach.gateway_port == 9555


def test_start_net_env_remote_branch() -> None:
    _enable_remote()
    with patch(
        "nika.remote.workflows.remote_start_net_env", return_value="remote-sess"
    ) as mocked:
        from nika.workflows.env.start import start_net_env

        sid = start_net_env("simple_bgp", None, static_validation=True)
    assert sid == "remote-sess"
    mocked.assert_called_once()
    assert mocked.call_args.kwargs["static_validation"] is True


def test_mcp_policy_resource_from_url() -> None:
    from agent.sandbox.sbx.policy import mcp_policy_resource_from_url

    assert mcp_policy_resource_from_url("http://10.1.2.3:9444/mcp") == "10.1.2.3:9444"
