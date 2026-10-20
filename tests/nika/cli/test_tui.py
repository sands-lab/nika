from __future__ import annotations

import httpx
import pytest

from nika.agents import endpoint_probe
from nika.agents.endpoint_probe import probe_endpoint
from nika.tui.app import Form

pytestmark = pytest.mark.unit


def _serve(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    transport = httpx.MockTransport(handler)

    def fake_get(url, **kwargs):
        kwargs.pop("timeout", None)
        with httpx.Client(transport=transport) as client:
            return client.get(url, **kwargs)

    monkeypatch.setattr(endpoint_probe.httpx, "get", fake_get)


@pytest.mark.parametrize(
    ("model", "status_code", "expected"),
    [
        ("qwen3-32b", 200, "ok"),
        ("claude-sonnet-4-5", 200, "ok"),  # alias of a dated id
        ("v4-flash", 200, "ok"),  # router-prefixed id
        ("missing", 200, "model_not_listed"),
        ("qwen3-32b", 401, "auth_failed"),
    ],
)
def test_probe_classifies_endpoint(monkeypatch, model, status_code, expected) -> None:
    models = ["qwen3-32b", "claude-sonnet-4-5-20250929", "deepseek/v4-flash"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        assert request.headers["Authorization"] == "Bearer secret"
        return httpx.Response(status_code, json={"data": [{"id": m} for m in models]})

    _serve(monkeypatch, handler)
    result = probe_endpoint(
        "custom",
        model,
        base_url="http://llm.local/v1",
        sources={"NIKA_CUSTOM_API_KEY": "secret"},
    )
    assert result.status == expected
    assert result.ok == (expected != "auth_failed")


def test_probe_reports_missing_credentials_and_unreachable(monkeypatch) -> None:
    assert probe_endpoint("openai", "gpt-5", sources={}).status == "no_credentials"
    assert probe_endpoint("custom", "m", sources={}).status == "no_credentials"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    _serve(monkeypatch, handler)
    result = probe_endpoint("custom", "m", base_url="http://down.local/v1", sources={})
    assert result.status == "unreachable"
    assert not result.ok


def test_form_renders_cli_argv() -> None:
    from nika.cli.main import app
    from typer.main import get_command
    import click

    root = get_command(app)
    ctx = click.Context(root, info_name="nika")
    agent_run = root.get_command(ctx, "agent").get_command(ctx, "run")
    form = Form(
        ["agent", "run"],
        agent_run,
        {
            "problem": "dc_clos_s_link_down",
            "sets": ["host_name=leaf1", "intf_name=eth1"],
            "sandbox_keep_container": True,
            "sandbox_offline_sdk_wheels": False,
            "model": "",
        },
    )
    assert form.argv() == [
        "agent",
        "run",
        "--problem",
        "dc_clos_s_link_down",
        "--set",
        "host_name=leaf1",
        "--set",
        "intf_name=eth1",
        "--sandbox-keep-container",
        "--no-sandbox-offline-sdk-wheels",
    ]

    exec_cmd = root.get_command(ctx, "exec")
    form = Form(
        ["exec"],
        exec_cmd,
        {"host": "pc1", "command_parts": ["ip", "route"], "session_id": "s1"},
    )
    assert [p.name for p in form.missing()] == []
    # exec stops option parsing at HOST, so options must come first.
    assert form.argv() == ["exec", "--session_id", "s1", "pc1", "ip", "route"]
    assert [p.name for p in Form(["exec"], exec_cmd).missing()] == [
        "host",
        "command_parts",
    ]
