"""Static HTTP clients retain file, directory, and partial-download behavior."""

from functools import partial
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from threading import Thread

import pytest

from nika.net_env.utils.kathara.static_httpd import Handler

pytestmark = pytest.mark.unit


@pytest.fixture
def http_request(tmp_path):
    (tmp_path / "large.bin").write_bytes(bytes(range(256)) * 1024)
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "notes.txt").write_text("archive entry\n")
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(Handler, directory=str(tmp_path))
    )
    thread = Thread(target=server.serve_forever)
    thread.start()

    def fetch(path, *, method="GET", headers=None):
        connection = HTTPConnection(*server.server_address, timeout=5)
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

    try:
        yield fetch
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


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
