"""Host side of the sandbox tunnel (see :mod:`agent.sandbox.tunnel_relay`).

Sandbox runtimes such as AgentENV block sandbox traffic to the host. The relay
inside the sandbox accepts connections on loopback ports and forwards them as
frames over a channel the host already has (a command's stdin/stdout). The
bridge here connects each one to the host service mapped to its port.
Connections on unmapped ports are refused, so a sandbox reaches exactly the
services of its own session.
"""

from __future__ import annotations

import base64
import json
import logging
import socket
import threading
from collections.abc import Callable, Mapping
from pathlib import Path

logger = logging.getLogger(__name__)

RELAY_SCRIPT = Path(__file__).resolve().with_name("tunnel_relay.py")
_CHUNK = 64 * 1024
_CONNECT_TIMEOUT_SEC = 10


class TunnelBridge:
    """Connect relay connections to host services.

    *targets* maps each relay port to the host ``(host, port)`` it reaches.
    *send* delivers one frame line to the relay's stdin; pass the relay's
    stdout to :meth:`feed` as it arrives.
    """

    def __init__(
        self,
        targets: Mapping[int, tuple[str, int]],
        send: Callable[[str], None],
    ) -> None:
        self._targets = {int(port): target for port, target in targets.items()}
        self._send = send
        self._send_lock = threading.Lock()
        self._conns: dict[int, socket.socket] = {}
        self._conns_lock = threading.Lock()
        self._pending = ""
        self._closed = False
        self.ready = threading.Event()

    def feed(self, chunk: str) -> None:
        """Handle relay stdout, which may split or join frame lines."""
        self._pending += chunk
        *lines, self._pending = self._pending.split("\n")
        for line in lines:
            if line.strip():
                self._dispatch(line)

    def close(self) -> None:
        """Stop forwarding and close every host connection."""
        self._closed = True
        with self._conns_lock:
            conns = list(self._conns.values())
            self._conns.clear()
        for sock in conns:
            _close_socket(sock)

    def _dispatch(self, line: str) -> None:
        try:
            frame = json.loads(line)
        except ValueError:
            logger.debug("Ignoring non-frame relay output: %s", line[:200])
            return
        if not isinstance(frame, dict):
            return
        kind = frame.get("t")
        if kind == "ready":
            self.ready.set()
            return
        try:
            conn_id = int(frame.get("id"))
        except (TypeError, ValueError):
            return
        if kind == "open":
            self._open(conn_id, frame.get("port"))
        elif kind == "data":
            sock = self._conns.get(conn_id)
            if sock is None:
                return
            try:
                sock.sendall(base64.b64decode(frame.get("b", "")))
            except (OSError, ValueError):
                self._drop(conn_id, notify=True)
        elif kind == "close":
            self._drop(conn_id, notify=False)

    def _open(self, conn_id: int, port: object) -> None:
        try:
            target = self._targets.get(int(port))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            target = None
        if target is None or self._closed:
            self._send_frame({"t": "close", "id": conn_id})
            return
        try:
            sock = socket.create_connection(target, timeout=_CONNECT_TIMEOUT_SEC)
        except OSError as exc:
            logger.warning("Tunnel could not reach %s:%s: %s", *target, exc)
            self._send_frame({"t": "close", "id": conn_id})
            return
        sock.settimeout(None)
        with self._conns_lock:
            self._conns[conn_id] = sock
        threading.Thread(
            target=self._pump,
            args=(conn_id, sock),
            name=f"nika-tunnel-{conn_id}",
            daemon=True,
        ).start()

    def _pump(self, conn_id: int, sock: socket.socket) -> None:
        try:
            while chunk := sock.recv(_CHUNK):
                self._send_frame(
                    {"t": "data", "id": conn_id, "b": base64.b64encode(chunk).decode()}
                )
        except OSError:
            pass
        self._drop(conn_id, notify=True)

    def _drop(self, conn_id: int, *, notify: bool) -> None:
        with self._conns_lock:
            sock = self._conns.pop(conn_id, None)
        if sock is None:
            return
        _close_socket(sock)
        if notify:
            self._send_frame({"t": "close", "id": conn_id})

    def _send_frame(self, frame: dict) -> None:
        if self._closed:
            return
        line = json.dumps(frame, separators=(",", ":")) + "\n"
        with self._send_lock:
            try:
                self._send(line)
            except Exception as exc:  # noqa: BLE001 - a dead channel ends the tunnel
                logger.warning("Tunnel could not reach the sandbox relay: %s", exc)


def _close_socket(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()
