"""Static HTTP clients retain file, directory, and partial-download behavior."""

from functools import partial
import json
import sys
from http.client import HTTPConnection, RemoteDisconnected
from http.server import BaseHTTPRequestHandler
from queue import Queue, Empty
from threading import Thread
from threading import Event

import pytest

from nika.net_env.utils.kathara.static_httpd import BoundedHTTPServer, Handler

pytestmark = pytest.mark.unit


@pytest.fixture
def http_server(tmp_path):
    (tmp_path / "large.bin").write_bytes(bytes(range(256)) * 1024)
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "notes.txt").write_text("archive entry\n")
    server = BoundedHTTPServer(
        ("127.0.0.1", 0), partial(Handler, directory=str(tmp_path))
    )
    thread = Thread(target=server.serve_forever)
    thread.start()

    try:
        yield server
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.fixture
def http_request(http_server):
    def fetch(path, *, method="GET", headers=None):
        connection = HTTPConnection(*http_server.server_address, timeout=5)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return (
                response.status,
                {key.lower(): value for key, value in response.getheaders()},
                response.read(),
            )
        finally:
            connection.close()

    return fetch


@pytest.mark.parametrize(
    "path,headers",
    [
        ("/large.bin", {}),
        ("/archive/", {}),
        ("/large.bin", {"Range": "bytes=0-3"}),
        ("/large.bin", {"Range": "bytes=999999-"}),
    ],
)
def test_repeated_requests_keep_the_same_tcp_connection(http_server, path, headers):
    connection = HTTPConnection(*http_server.server_address, timeout=5)
    try:
        connection.connect()
        original_socket = connection.sock
        for _ in range(2):
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            response.read()
            assert response.version == 11
            assert not response.will_close
            assert connection.sock is original_socket
    finally:
        connection.close()


def test_head_types_and_directory_listing(http_request):
    status, headers, body = http_request("/large.bin", method="HEAD")
    assert status == 200
    assert headers["content-length"] == str(256 * 1024)
    assert body == b""
    status, headers, body = http_request("/archive/")
    assert status == 200
    assert headers["content-type"].startswith("text/html")
    assert b"notes.txt" in body
    status, headers, body = http_request("/archive/notes.txt")
    assert status == 200
    assert headers["content-type"] == "text/plain"
    assert body == b"archive entry\n"


@pytest.mark.parametrize(
    "value,start,end",
    [
        ("bytes=0-131071", 0, 131071),
        ("bytes=262140-", 262140, 262143),
        ("bytes=-4", 262140, 262143),
    ],
)
def test_single_range_returns_exact_slice(http_request, value, start, end):
    status, headers, body = http_request("/large.bin", headers={"Range": value})
    assert status == 206
    assert headers["content-range"] == f"bytes {start}-{end}/262144"
    assert headers["content-length"] == str(end - start + 1)
    assert body == (bytes(range(256)) * 1024)[start : end + 1]
    status, headers, body = http_request(
        "/large.bin", method="HEAD", headers={"Range": value}
    )
    assert status == 206
    assert headers["content-length"] == str(end - start + 1)
    assert body == b""


@pytest.mark.parametrize("value", ["bytes=262144-", "bytes=12-5", "bytes=-0"])
def test_unsatisfiable_range(http_request, value):
    status, headers, body = http_request("/large.bin", headers={"Range": value})
    assert status == 416
    assert headers["content-range"] == "bytes */262144"
    assert body == b""


@pytest.mark.parametrize(
    "protocol,expected", [("HTTP/1.0", "broken"), ("HTTP/1.1", "alive")]
)
def test_lb_probe_rejects_closed_connections(
    http_server, tmp_path, monkeypatch, protocol, expected
):
    import http.client
    from nika.validation.symptom.p4_gateway_probes import _LB_KEEPALIVE

    monkeypatch.setattr(Handler, "protocol_version", protocol)
    connection_type = http.client.HTTPConnection

    def connection(_host, _port, timeout, source_address):
        assert source_address == ("", 0)
        return connection_type(
            *http_server.server_address, timeout=timeout, source_address=source_address
        )

    monkeypatch.setattr(http.client, "HTTPConnection", connection)
    status = tmp_path / "flows.json"
    monkeypatch.setattr(
        sys, "argv", ["probe", "unused", "2", str(status), "0.01", "0.1"]
    )
    exec(_LB_KEEPALIVE, {})
    flows = json.loads(status.read_text())["flows"]
    assert len(flows) == 2
    assert set(flows.values()) == {expected}


def test_request_capacity_rejects_excess_and_recovers():
    entered = Queue()
    release = {str(i): Event() for i in range(3)}

    class SmallServer(BoundedHTTPServer):
        max_handlers = 2

    class WaitingHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            key = self.path.strip("/")
            entered.put(key)
            release[key].wait(timeout=5)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, *_):
            pass

    server = SmallServer(("127.0.0.1", 0), WaitingHandler)
    thread = Thread(target=server.serve_forever)
    thread.start()
    clients = [HTTPConnection(*server.server_address, timeout=5) for _ in range(3)]
    try:
        for i in range(2):
            clients[i].request("GET", "/" + str(i))
            assert entered.get(timeout=2) == str(i)
        clients[2].request("GET", "/2")
        with pytest.raises((RemoteDisconnected, ConnectionResetError)):
            clients[2].getresponse()
        with pytest.raises(Empty):
            entered.get(timeout=0.1)
        release["0"].set()
        assert clients[0].getresponse().read() == b"OK"
        clients[2].close()
        clients[2] = HTTPConnection(*server.server_address, timeout=5)
        clients[2].request("GET", "/2")
        assert entered.get(timeout=2) == "2"
        for i in [1, 2]:
            release[str(i)].set()
            assert clients[i].getresponse().read() == b"OK"
    finally:
        for event in release.values():
            event.set()
        for client in clients:
            client.close()
        server.shutdown()
        thread.join()
        server.server_close()
