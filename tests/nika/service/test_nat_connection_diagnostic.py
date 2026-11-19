"""Connection diagnostics own their sockets, process identity and temporary files."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import tempfile
import threading

import pytest

from nika.problems.support.nat_sessions import (
    TCP_BURST,
    start_connections,
    stop_connections,
)

pytestmark = pytest.mark.integration


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_):
        pass


def _run(command):
    return subprocess.run(
        command, shell=True, text=True, capture_output=True, timeout=20
    )


@pytest.mark.parametrize("accept", [True, False])
def test_owned_connections_close_after_success_or_connection_failure(accept):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    if not accept:
        server.shutdown()
        server.server_close()
        thread.join()
    record = None
    try:
        result = _run(start_connections("127.0.0.1", port, 4))
        assert result.returncode == 0, result.stderr
        record = json.loads(result.stdout)
        assert record["connected"] == (4 if accept else 0)
        assert record["failed"] == (0 if accept else 4)
        assert Path(record["directory"]).is_dir()
        result = _run(stop_connections(json.dumps(record)))
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["stopped"] is True
        assert not Path(record["directory"]).exists()
        assert json.loads(_run(stop_connections(json.dumps(record))).stdout)["stopped"]
    finally:
        if record and Path(record["directory"]).exists():
            _run(stop_connections(json.dumps(record)))
        if accept:
            server.shutdown()
            server.server_close()
            thread.join()


def test_cleanup_rejects_an_unrelated_process_and_preserves_its_files():
    with tempfile.TemporaryDirectory(prefix="nat-check-", dir="/tmp") as name:
        sentinel = Path(name) / "unrelated"
        sentinel.write_text("preserve")
        result = _run(
            stop_connections(json.dumps({"directory": name, "pid": os.getpid()}))
        )
        assert result.returncode != 0
        assert "identity changed" in result.stderr
        assert sentinel.read_text() == "preserve"


def test_expired_connections_remove_only_their_owned_directory(tmp_path):
    import sys

    owned = tmp_path / "owned"
    owned.mkdir()
    sentinel = tmp_path / "unrelated"
    sentinel.write_text("preserve")
    result = subprocess.run(
        [sys.executable, "-c", TCP_BURST, "127.0.0.1", "1", "2", "0.1", str(owned)],
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["failed"] == 2
    assert not owned.exists()
    assert sentinel.read_text() == "preserve"
