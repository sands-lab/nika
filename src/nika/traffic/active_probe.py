"""Small deterministic TCP probes for active data-plane diagnosis."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import shlex
import textwrap

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
    destination_address: str | None = None,
) -> dict:
    """Send seeded payload records over one caller-selected TCP 5-tuple.

    The listener runs on node *destination*; the client connects to
    *destination_address* when given, else to that node's data-plane address.
    """
    if not (1 <= source_port <= 65535 and 1 <= destination_port <= 65535):
        raise ValueError("TCP ports must be in 1..65535")
    if payload_size < 33 or packets < 1:
        raise ValueError("payload_size must be >= 33 and packets must be positive")
    for argument, node in (("source", source), ("destination", destination)):
        try:
            ipaddress.ip_address(node)
        except ValueError:
            continue
        raise ValueError(
            f"{argument} must be a lab node name; use destination_address for an IP"
        )
    payload = hashlib.shake_256(f"{payload_seed}".encode()).digest(payload_size)
    encoded = base64.b64encode(payload).decode()
    if destination_address:
        destination_ip = str(ipaddress.ip_address(destination_address))
    else:
        destination_ip = runtime.get_data_plane_host_ip(destination)
        if not destination_ip:
            raise ValueError(f"Cannot resolve IP for host {destination!r}")
    if ipaddress.ip_address(destination_ip).version == 6:
        family, wildcard = "socket.AF_INET6", "::"
    else:
        family, wildcard = "socket.AF_INET", "0.0.0.0"
    result = {
        "source": source,
        "destination": destination,
        "destination_address": destination_ip,
        "source_port": source_port,
        "destination_port": destination_port,
        "payload_size": payload_size,
        "packets": packets,
    }
    for node in dict.fromkeys((source, destination)):
        available = runtime.exec(
            node, "command -v python3 >/dev/null 2>&1 && echo ready", timeout=5
        )
        if available.strip() != "ready":
            return {
                **result,
                "status": "unsupported",
                "error": f"Node {node!r} needs python3 for active TCP probing",
            }
    # The listener gives up on its own if no client arrives, so a failed
    # probe never leaves a process holding the port.
    server = f"""import socket, sys
s = socket.socket(fileno=int(sys.argv[1]))
s.settimeout({_SERVER_WAIT_SEC})
c, _ = s.accept()
c.settimeout(5)
n = 0
for _ in range({packets}):
    remaining = {payload_size}
    while remaining:
        data = c.recv(remaining)
        if not data:
            break
        remaining -= len(data)
    if remaining:
        break
    n += 1
    c.sendall(b'1')
print(n)
c.close()
s.close()
"""
    launch = f"""import json, socket, subprocess, sys
s = socket.socket({family})
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(({wildcard!r}, {destination_port}))
    s.listen(1)
    subprocess.Popen([sys.executable, '-c', {server!r}, str(s.fileno())],
                     pass_fds=(s.fileno(),), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    print(json.dumps({{'ready': True}}))
except OSError as exc:
    print(json.dumps({{'error': str(exc)}}))
finally:
    s.close()
"""
    output = runtime.exec(destination, f"python3 -c {shlex.quote(launch)}", timeout=5)
    try:
        listener = json.loads(output.strip())
    except ValueError:
        listener = {"error": "Node did not return a valid listener result"}
    if not isinstance(listener, dict) or listener.get("ready") is not True:
        return {**result, "status": "unavailable", "listener": listener}
    # The listener starts in the background; retry refused connects until it
    # is up instead of guessing a startup delay.
    client = f"""import base64, socket, time
p = base64.b64decode({encoded!r})
deadline = time.monotonic() + {_CONNECT_RETRY_SEC}
while True:
    s = socket.socket({family})
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.settimeout(5)
    s.bind(({wildcard!r}, {source_port}))
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
print(json.dumps({{'acked': n, 'elapsed_ms': round((time.monotonic() - t) * 1000, 3)}}))
s.close()
"""
    client = (
        "import json\ntry:\n"
        + textwrap.indent(client, "    ")
        + "except OSError as exc:\n    print(json.dumps({'error': str(exc)}))\n"
    )
    output = runtime.exec(source, f"python3 -c {shlex.quote(client)}", timeout=30)
    try:
        reading = json.loads(output.strip())
    except ValueError:
        return {
            **result,
            "status": "failed",
            "error": "Node did not return a valid TCP probe result",
        }
    if not isinstance(reading, dict):
        return {
            **result,
            "status": "failed",
            "error": "Node did not return a TCP probe object",
        }
    return {
        **result,
        "status": "ok" if reading.get("acked") == packets else "failed",
        "result": reading,
    }
