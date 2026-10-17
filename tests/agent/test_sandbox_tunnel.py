"""Host tunnel to sandboxes without a network route to the host."""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent.sandbox.tunnel import RELAY_SCRIPT, TunnelBridge
from nika.utils.net import pick_free_port

pytestmark = pytest.mark.unit


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"x" * 300_000  # spans many relay frames
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def _serve_byte_count() -> socket.socket:
    """Reply with the number of bytes received once the client stops sending."""
    listener = socket.create_server(("127.0.0.1", 0))

    def serve() -> None:
        conn, _ = listener.accept()
        with conn:
            received = 0
            while chunk := conn.recv(65536):
                received += len(chunk)
            conn.sendall(str(received).encode())

    threading.Thread(target=serve, daemon=True).start()
    return listener


def test_tunnel_reaches_only_mapped_host_services() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    counter = _serve_byte_count()
    mapped, unmapped, counted = (pick_free_port("127.0.0.1") for _ in range(3))
    relay = subprocess.Popen(
        [
            sys.executable,
            "-u",
            str(RELAY_SCRIPT),
            str(mapped),
            str(unmapped),
            str(counted),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert relay.stdin is not None and relay.stdout is not None

    def send(line: str) -> None:
        relay.stdin.write(line)
        relay.stdin.flush()

    bridge = TunnelBridge(
        {mapped: server.server_address, counted: counter.getsockname()}, send
    )

    def pump() -> None:
        for line in relay.stdout:
            bridge.feed(line)

    threading.Thread(target=pump, daemon=True).start()
    try:
        assert bridge.ready.wait(10)
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            f"http://127.0.0.1:{mapped}/", timeout=10
        ) as resp:
            assert resp.status == 200
            assert len(resp.read()) == 300_000

        # A client that stops sending still gets the reply.
        with socket.create_connection(("127.0.0.1", counted), timeout=10) as sock:
            sock.sendall(b"x" * 200_000)
            sock.shutdown(socket.SHUT_WR)
            reply = b""
            while chunk := sock.recv(1024):
                reply += chunk
            assert reply == b"200000"

        # A relay port without a host target is closed without data.
        with socket.create_connection(("127.0.0.1", unmapped), timeout=10) as sock:
            sock.sendall(b"GET / HTTP/1.0\r\n\r\n")
            assert sock.recv(1024) == b""

        # The relay listens on nothing else.
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", pick_free_port("127.0.0.1")), 1)
    finally:
        bridge.close()
        relay.kill()
        relay.wait(10)
        server.shutdown()
        server.server_close()
        counter.close()
