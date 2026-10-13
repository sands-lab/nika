"""Small deterministic TCP probes for active data-plane diagnosis."""

from __future__ import annotations

import base64
import hashlib
import shlex

from nika.runtime.base import LabRuntime

_SERVER_WAIT_SEC = 20
_CONNECT_RETRY_SEC = 5


def run_active_tcp_probe(
    runtime: LabRuntime,
    *,
    source: str,
    destination: str,
    source_port: int,
    destination_port: int,
    payload_seed: int,
    payload_size: int = 256,
    packets: int = 32,
) -> dict:
    """Send seeded payload records over one caller-selected TCP 5-tuple."""
    if not (1 <= source_port <= 65535 and 1 <= destination_port <= 65535):
        raise ValueError("TCP ports must be in 1..65535")
    if payload_size < 33 or packets < 1:
        raise ValueError("payload_size must be >= 33 and packets must be positive")
    payload = hashlib.shake_256(f"{payload_seed}".encode()).digest(payload_size)
    encoded = base64.b64encode(payload).decode()
    destination_ip = runtime.get_data_plane_host_ip(destination)
    if not destination_ip:
        raise ValueError(f"Cannot resolve IP for host {destination!r}")
    # The listener gives up on its own if no client arrives, so a failed
    # probe never leaves a process holding the port.
    server = f"""import socket
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.settimeout({_SERVER_WAIT_SEC})
s.bind(('0.0.0.0', {destination_port}))
s.listen(1)
c, _ = s.accept()
c.settimeout(5)
n = 0
for _ in range({packets}):
    data = c.recv({payload_size})
    if not data:
        break
    n += 1
    c.sendall(b'1')
print(n)
c.close()
s.close()
"""
    limit = (
        f"$(command -v timeout >/dev/null 2>&1 && echo timeout {_SERVER_WAIT_SEC + 10})"
    )
    runtime.exec(
        destination,
        f"{limit} python3 -c {shlex.quote(server)} >/tmp/nika-probe-server.log 2>&1 &",
    )
    # The listener starts in the background; retry refused connects until it
    # is up instead of guessing a startup delay.
    client = f"""import base64, socket, time
p = base64.b64decode({encoded!r})
deadline = time.monotonic() + {_CONNECT_RETRY_SEC}
while True:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.settimeout(5)
    s.bind(('0.0.0.0', {source_port}))
    t = time.monotonic()
    try:
        s.connect(({destination_ip!r}, {destination_port}))
        break
    except ConnectionRefusedError:
        s.close()
        if time.monotonic() > deadline:
            raise
        time.sleep(0.05)
n = 0
for _ in range({packets}):
    s.sendall(p)
    n += bool(s.recv(1))
print({{'acked': n, 'elapsed_ms': round((time.monotonic() - t) * 1000, 3)}})
s.close()
"""
    output = runtime.exec(source, f"python3 -c {shlex.quote(client)}", timeout=30)
    return {
        "source": source,
        "destination": destination,
        "source_port": source_port,
        "destination_port": destination_port,
        "payload_size": payload_size,
        "packets": packets,
        "result": output.strip(),
    }
