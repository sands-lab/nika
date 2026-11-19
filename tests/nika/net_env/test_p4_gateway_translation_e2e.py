"""VIP reverse translation survives concurrent direct service connections."""

import shlex

import pytest

from nika.runtime.factory import runtime_for_session
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available

pytestmark = pytest.mark.e2e

# These different client ports collided in the former 4096-slot CRC32 cache.
_CONCURRENT_REQUESTS = """
import http.client, socket, sys, time
reuse = sys.argv[1] == 'reuse'
port = 20001 if reuse else 20000
vip = http.client.HTTPConnection(
    '20.0.0.1', 80, timeout=3, source_address=('192.0.2.11', port)
)
direct = socket.socket()
try:
    if reuse:
        vip.sock = socket.socket()
        vip.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        vip.sock.settimeout(3)
        vip.sock.bind(('192.0.2.11', port))
        vip.sock.connect(('20.0.0.1', 80))
        direct.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    vip.request('GET', '/')
    baseline = vip.getresponse()
    backend = baseline.read().decode().strip()
    assert baseline.status == 200
    target = '10.0.1.12' if backend == 'service_1_1' else '10.0.1.11'
    time.sleep(1)
    direct.bind(('192.0.2.11', port if reuse else 22597))
    direct.setblocking(False)
    vip.request('GET', '/')
    direct.connect_ex((target, 80))
    reply = vip.getresponse()
    reply.read()
    assert reply.status == 200
    direct.settimeout(3)
    direct.sendall(b'GET / HTTP/1.1\\r\\nHost: service\\r\\nConnection: close\\r\\n\\r\\n')
    assert direct.recv(4096).startswith(b'HTTP/1.1 200')
    print('VIP and direct service requests both returned HTTP 200')
finally:
    direct.close()
    vip.close()
"""


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class P4GatewayTranslationE2ETest(IntegrationTestCase):
    def test_direct_connection_preserves_learned_vip_connection(self):
        session_id = self._start_env("p4_dc_gateway", ["-s", "s"])
        try:
            meta = self._assert_session_ready(session_id, "p4_dc_gateway")
            runtime = runtime_for_session(meta)
            for mode in ("hash", "reuse"):
                output = runtime.exec(
                    "client_1",
                    "python3 -c " + shlex.quote(_CONCURRENT_REQUESTS) + " " + mode,
                    timeout=15,
                )
                assert (
                    "VIP and direct service requests both returned HTTP 200" in output
                ), output
        finally:
            self._close_session(session_id)
