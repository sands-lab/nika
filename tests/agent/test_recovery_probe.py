from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from nika.agents.endpoint_probe import probe_agent_generation

pytestmark = pytest.mark.integration


@contextmanager
def endpoint(response_payload=None):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, dict(self.headers), body))
            self.send_response(200)
            if body.get("stream"):
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                event = response_payload or {
                    "type": "response.output_text.delta",
                    "delta": "OK",
                }
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                return
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if self.path.endswith("messages"):
                result = {
                    "type": "message",
                    "content": [{"type": "text", "text": "OK"}],
                }
            elif self.path.endswith("responses"):
                result = {"object": "response", "status": "completed", "output": []}
            else:
                result = {
                    "choices": [{"message": {"role": "assistant", "content": "OK"}}]
                }
            self.wfile.write(json.dumps(response_payload or result).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(
    "agent,path",
    [
        ("byo.langgraph", "/v1/chat/completions"),
        ("cli.codex", "/v1/responses"),
        ("cli.claude", "/v1/messages"),
    ],
)
def test_generation_probe_uses_the_agent_protocol_and_credentials(
    monkeypatch, tmp_path, agent, path
):
    with endpoint() as (url, calls):
        monkeypatch.setenv("NIKA_CUSTOM_BASE_URL", url + "/v1")
        monkeypatch.setenv("NIKA_CUSTOM_API_KEY", "test-key")
        probe_agent_generation(
            agent, "custom", "test", env_file=tmp_path / "empty.env", timeout=2
        )
    assert len(calls) == 1
    assert calls[0][0] == path
    headers = {k.lower(): v for k, v in calls[0][1].items()}
    assert (
        headers.get("authorization") == "Bearer test-key"
        or headers.get("x-api-key") == "test-key"
    )


@pytest.mark.parametrize(
    "status,accepted", [("completed", True), ("incomplete", True), ("failed", False)]
)
def test_responses_probe_distinguishes_token_limit_from_failure(
    monkeypatch, tmp_path, status, accepted
):
    payload = {
        "object": "response",
        "status": status,
        "incomplete_details": {"reason": "max_output_tokens"},
    }
    with endpoint(payload) as (url, _):
        monkeypatch.setenv("NIKA_CUSTOM_BASE_URL", url + "/v1")
        monkeypatch.setenv("NIKA_CUSTOM_API_KEY", "test-key")
        if accepted:
            probe_agent_generation(
                "cli.codex",
                "custom",
                "test",
                env_file=tmp_path / "empty.env",
                timeout=2,
            )
        else:
            with pytest.raises(RuntimeError, match="no valid model response"):
                probe_agent_generation(
                    "cli.codex",
                    "custom",
                    "test",
                    env_file=tmp_path / "empty.env",
                    timeout=2,
                )


@pytest.mark.parametrize(
    "agent,provider", [("cli.codex", "openai"), ("cli.claude", "anthropic")]
)
def test_subscription_probe_reuses_the_proxy_upstream(
    monkeypatch, tmp_path, agent, provider
):
    from nika.agents.sandbox import model_proxy

    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    with endpoint() as (url, calls):
        monkeypatch.setattr(
            model_proxy,
            "_subscription_upstream",
            lambda **kwargs: (
                url,
                lambda: "test-oauth",
                {"test-account-id": "test-account"},
            ),
        )
        probe_agent_generation(
            agent, provider, "test", env_file=tmp_path / "empty.env", timeout=2
        )
    headers = {k.lower(): v for k, v in calls[0][1].items()}
    assert headers["authorization"] == "Bearer test-oauth"
    assert headers["test-account-id"] == "test-account"
    if provider == "openai":
        assert calls[0][0] == "/responses"
        assert calls[0][2]["stream"] is True
        assert "max_output_tokens" not in calls[0][2]
