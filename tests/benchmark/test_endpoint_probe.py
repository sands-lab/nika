import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nika.workflows.benchmark.endpoint import probe_model

pytestmark = pytest.mark.integration


def test_probe_requires_a_model_response(monkeypatch):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        healthy = False

        def do_POST(self):
            calls.append(
                json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            )
            self.send_response(200 if self.healthy else 503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "id": "probe",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "test",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": "OK"},
                            }
                        ],
                    }
                ).encode()
            )

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(
        "NIKA_CUSTOM_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1"
    )
    try:
        with pytest.raises(Exception):
            probe_model("custom", "test", 2)
        assert len(calls) == 1
        Handler.healthy = True
        probe_model("custom", "test", 2)
        assert len(calls) == 2
        assert calls[-1]["max_tokens"] == 1
        assert calls[-1]["stream"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_hung_probe_is_bounded_and_reaps_its_process(monkeypatch):
    import multiprocessing
    import time

    received = threading.Event()
    release = threading.Event()

    class Hung(BaseHTTPRequestHandler):
        def do_POST(self):
            received.set()
            release.wait(10)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Hung)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(
        "NIKA_CUSTOM_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1"
    )
    before = time.monotonic()
    children = {p.pid for p in multiprocessing.active_children()}
    try:
        with pytest.raises(Exception):
            probe_model("custom", "test", 2)
        assert received.is_set()
        assert time.monotonic() - before < 3
        assert {p.pid for p in multiprocessing.active_children()} == children
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()
