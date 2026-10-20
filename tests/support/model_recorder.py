"""Record what agents send to their model, as a test stand-in endpoint.

:class:`ModelRecorder` is a reverse proxy in front of an OpenAI/Anthropic
compatible endpoint. Every request body that reaches the model holds what the
agent saw (system prompt, tool definitions, tool results), so tests can check
the agent's view without instrumenting the agent.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

# Hop-by-hop headers, and framing the proxy sets itself.
_SKIP_HEADERS = frozenset(
    {"host", "connection", "content-length", "transfer-encoding", "keep-alive"}
)


class ModelRecorder:
    """Forward ``/v1/...`` requests to *upstream* and keep their bodies.

    *upstream* is the endpoint root without ``/v1``. Use :attr:`base_url` as
    ``agent.base_url``.
    """

    def __init__(self, upstream: str) -> None:
        self.upstream = upstream.rstrip("/").removesuffix("/v1")
        self.requests: list[tuple[str, bytes]] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __enter__(self) -> ModelRecorder:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def bodies(self) -> list[bytes]:
        with self._lock:
            return [body for _, body in self.requests]

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802
                self._forward()

            def do_POST(self) -> None:  # noqa: N802
                self._forward()

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def _forward(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                with recorder._lock:
                    recorder.requests.append((self.path, body))
                headers = {
                    key: value
                    for key, value in self.headers.items()
                    if key.lower() not in _SKIP_HEADERS
                }
                with httpx.Client(timeout=None) as client:
                    with client.stream(
                        self.command,
                        recorder.upstream + self.path,
                        headers=headers,
                        content=body,
                    ) as response:
                        self.send_response(response.status_code)
                        for key, value in response.headers.items():
                            if key.lower() not in _SKIP_HEADERS | {"content-encoding"}:
                                self.send_header(key, value)
                        self.send_header("Transfer-Encoding", "chunked")
                        self.end_headers()
                        # Stream through, so streamed completions stay streamed.
                        for chunk in response.iter_bytes():
                            if chunk:
                                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                                self.wfile.flush()
                        self.wfile.write(b"0\r\n\r\n")

        return Handler
