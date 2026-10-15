"""Reach services in a VM over vsock, without any guest network identity.

Restored copies of one snapshot share the guest's MAC and IP, but each has its
own vsock CID. Python builds without ``AF_VSOCK`` support (e.g. uv's) can still
create the socket; only the address parsing is missing, so ``connect`` goes
through libc directly.
"""

from __future__ import annotations

import ctypes
import os
import socket
import threading
import time

AF_VSOCK = getattr(socket, "AF_VSOCK", 40)


class _SockaddrVm(ctypes.Structure):
    _fields_ = [
        ("svm_family", ctypes.c_ushort),
        ("svm_reserved1", ctypes.c_ushort),
        ("svm_port", ctypes.c_uint),
        ("svm_cid", ctypes.c_uint),
        ("svm_zero", ctypes.c_ubyte * 4),
    ]


_libc = ctypes.CDLL(None, use_errno=True)


def connect_vsock(cid: int, port: int, *, timeout: float = 10.0) -> socket.socket:
    """Return a connected stream socket to ``cid:port``."""
    sock = socket.socket(AF_VSOCK, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "AF_VSOCK"):
            sock.settimeout(timeout)
            sock.connect((cid, port))
        else:
            addr = _SockaddrVm(AF_VSOCK, 0, port, cid)
            if _libc.connect(sock.fileno(), ctypes.byref(addr), ctypes.sizeof(addr)):
                err = ctypes.get_errno()
                raise OSError(err, f"vsock connect {cid}:{port}: {os.strerror(err)}")
            sock.settimeout(timeout)
    except BaseException:
        sock.close()
        raise
    return sock


def http_ok(cid: int, port: int, path: str = "/health") -> bool:
    """True when ``GET path`` over vsock answers 200."""
    try:
        with connect_vsock(cid, port, timeout=3.0) as sock:
            sock.sendall(f"GET {path} HTTP/1.0\r\n\r\n".encode())
            return sock.recv(16).startswith(b"HTTP/1.1 200")
    except OSError:
        return False


def wait_http_ok(cid: int, port: int, *, timeout: float = 300.0) -> float:
    """Wait until ``http_ok``; return the seconds waited."""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if http_ok(cid, port):
            return time.monotonic() - started
        time.sleep(0.1)
    raise TimeoutError(f"no HTTP 200 on vsock {cid}:{port} within {timeout:.0f}s")


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while data := src.recv(65536):
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (src, dst):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class VsockForwarder:
    """Forward a local TCP port to ``cid:vsock_port`` until closed."""

    def __init__(self, cid: int, vsock_port: int, *, host: str = "127.0.0.1") -> None:
        self._cid = cid
        self._vsock_port = vsock_port
        self._server = socket.create_server((host, 0))
        self.port: int = self._server.getsockname()[1]
        self._closed = threading.Event()
        self._thread = threading.Thread(
            target=self._serve, name=f"vsock-fwd-{cid}-{vsock_port}", daemon=True
        )
        self._thread.start()

    def _serve(self) -> None:
        while not self._closed.is_set():
            try:
                client, _ = self._server.accept()
            except OSError:
                return
            try:
                upstream = connect_vsock(self._cid, self._vsock_port)
            except OSError:
                client.close()
                continue
            # Long agent tool calls must not hit the connect timeout.
            upstream.settimeout(None)
            for a, b in ((client, upstream), (upstream, client)):
                threading.Thread(target=_pipe, args=(a, b), daemon=True).start()

    def close(self) -> None:
        self._closed.set()
        try:
            self._server.close()
        except OSError:
            pass
