"""Benchmark contract: the host records model and tool calls, and counts steps.

``max_steps`` is a number of LLM requests per phase, counted and enforced by
the model proxy; ``model_calls.jsonl`` and ``tool_calls.jsonl`` are the
authoritative records the evaluator reads.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agent.protocols import DIAGNOSIS, SUBMISSION
from nika.agents.sandbox.model_calls import (
    ModelCallLog,
    model_call_metrics,
    response_text_and_usage,
)
from nika.agents.sandbox.model_proxy import create_model_proxy_app
from nika.evaluator.trace_parser import AgentTraceParser
from nika.mcp.gateway.tool_log import record_tool_call, tool_result

pytestmark = pytest.mark.unit

_CHAT = {
    "choices": [{"message": {"role": "assistant", "content": "link down"}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 4},
}


@pytest.fixture
def upstream() -> Iterator[str]:
    """OpenAI-compatible stand-in that answers every request with ``_CHAT``."""

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers.get("content-length") or 0))
            body = json.dumps(_CHAT).encode()
            self.send_response(503 if self.headers.get("x-overloaded") else 200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_proxy_records_calls_and_enforces_the_phase_budget(
    tmp_path: Path, upstream: str
) -> None:
    log = ModelCallLog(tmp_path / "model_calls.jsonl", max_requests=2)
    proxy = TestClient(create_model_proxy_app(upstream, token="t", call_log=log))
    headers = {"authorization": "Bearer t"}
    request = {"model": "m", "messages": [{"role": "user", "content": "diagnose"}]}

    # Token counting is not an LLM request.
    assert proxy.post("/v1/messages/count_tokens", headers=headers, json={}).is_success
    for _ in range(2):
        reply = proxy.post("/v1/chat/completions", headers=headers, json=request)
        assert reply.json() == _CHAT
    refused = proxy.post("/v1/chat/completions", headers=headers, json=request)
    assert refused.status_code == 400
    assert log.exhausted.is_set()
    assert log.last_text(DIAGNOSIS) == "link down"

    # Each phase has its own budget; requests the provider fails are free.
    log.set_phase(SUBMISSION)
    assert not log.exhausted.is_set()
    overloaded = {**headers, "x-overloaded": "1"}
    for _ in range(3):
        reply = proxy.post("/v1/chat/completions", headers=overloaded, json=request)
        assert reply.status_code == 503
    for _ in range(2):
        reply = proxy.post("/v1/chat/completions", headers=headers, json=request)
        assert reply.is_success

    rows = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert [(r["phase"], r["llm_request"], r["status"]) for r in rows] == [
        (DIAGNOSIS, False, 200),
        (DIAGNOSIS, True, 200),
        (DIAGNOSIS, True, 200),
        (DIAGNOSIS, True, 400),
        *[(SUBMISSION, True, 503)] * 3,
        *[(SUBMISSION, True, 200)] * 2,
    ]
    # The record keeps what the agent sent and received.
    assert rows[1]["request"] == request and rows[1]["response"] == _CHAT
    assert model_call_metrics(log.path) == {
        "steps": 2,
        "in_tokens": 20,
        "out_tokens": 8,
        "reasoning_tokens": 0,
    }


@pytest.mark.parametrize(
    "stream",
    [
        # Anthropic Messages
        [
            {"type": "message_start", "message": {"usage": {"input_tokens": 7}}},
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "lin"},
            },
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "k"},
            },
            {"type": "message_delta", "usage": {"output_tokens": 3}},
        ],
        # OpenAI Chat Completions
        [
            {"choices": [{"delta": {"content": "lin"}}]},
            {"choices": [{"delta": {"content": "k"}}]},
            {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}},
        ],
        # OpenAI Responses
        [
            {"type": "response.output_text.delta", "delta": "li"},
            {
                "type": "response.completed",
                "response": {
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "link"}],
                        }
                    ],
                    "usage": {"input_tokens": 7, "output_tokens": 3},
                },
            },
        ],
    ],
    ids=["messages", "chat", "responses"],
)
def test_streamed_replies_yield_text_and_usage(stream: list[dict]) -> None:
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in stream)
    text, usage = response_text_and_usage(body + "data: [DONE]\n\n", streamed=True)
    assert text == "link"
    assert usage and (usage.get("input_tokens") or usage.get("prompt_tokens")) == 7


def test_evaluator_prefers_the_host_records(tmp_path: Path) -> None:
    """The agent-written trace cannot raise or lower the scored counts."""
    trace = tmp_path / "messages.jsonl"
    trace.write_text(
        "".join(
            json.dumps({"timestamp": f"2026-01-01T00:00:0{i}+00:00", **event}) + "\n"
            for i, event in enumerate(
                [
                    {"phase": DIAGNOSIS, "event": "llm_end"},
                    {"phase": DIAGNOSIS, "event": "tool_start"},
                ]
            )
        )
    )
    log = ModelCallLog(tmp_path / "model_calls.jsonl")
    for _ in range(3):
        log.record(
            seq=1,
            phase=DIAGNOSIS,
            counted=True,
            method="POST",
            path="/v1/chat/completions",
            request=b"{}",
            status=200,
            response=json.dumps(_CHAT).encode(),
            streamed=False,
            started=0.0,
        )
    for is_error in (False, True):
        record_tool_call(
            str(tmp_path),
            phase=DIAGNOSIS,
            server="kathara_base_mcp_server",
            tool="exec_shell",
            arguments={"host_name": "pc1"},
            is_error=is_error,
            output="",
        )
    metrics = AgentTraceParser(str(trace)).parse_trace()
    assert (metrics["steps"], metrics["tool_calls"], metrics["tool_errors"]) == (
        3,
        2,
        1,
    )
    assert (metrics["in_tokens"], metrics["out_tokens"]) == (30, 12)
    assert metrics["time_taken"] == 1


def test_tool_result_reads_json_and_event_stream_bodies() -> None:
    ok = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": "up"}]},
    }
    failed = {"jsonrpc": "2.0", "id": 1, "result": {"isError": True, "content": []}}
    assert tool_result(json.dumps(ok).encode(), "application/json") == (False, "up")
    sse = f"event: message\ndata: {json.dumps(failed)}\n\n".encode()
    assert tool_result(sse, "text/event-stream")[0] is True
