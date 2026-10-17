"""Firecracker microVMs: process control, API, and the host side of vsock.

Every VM runs in its own directory. Its disk (``disk.img``) and vsock socket
(``vsock.sock``) are configured with paths relative to that directory, so a
snapshot restored by a Firecracker process started in another directory uses
that directory's files: copies of one snapshot never share a disk or socket.

Firecracker's vsock is "hybrid": the host side is a Unix socket on which a
client sends ``CONNECT <port>\\n`` and reads ``OK <n>\\n`` before the stream to
the guest port starts.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

DISK = "disk.img"
VSOCK = "vsock.sock"
API = "api.sock"
GUEST_CID = 3
EXEC_PORT = 9000  # nika-guest-exec in the base image
EXIT_MARK = b"\n__nika_exit="


class VmmError(RuntimeError):
    """A Firecracker API call or guest command failed."""


def firecracker_binary(configured: str) -> str:
    path = shutil.which(configured) or (
        configured if os.access(configured, os.X_OK) else None
    )
    if path is None:
        raise VmmError(
            f"Firecracker binary {configured!r} not found; "
            "see docs/operations/vm-snapshots.md."
        )
    return path


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


class Firecracker:
    """One Firecracker process serving the VM in ``run_dir``."""

    def __init__(self, run_dir: Path, binary: str) -> None:
        self.run_dir = Path(run_dir)
        self.binary = binary

    @property
    def pid_file(self) -> Path:
        return self.run_dir / "firecracker.pid"

    def start(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        for name in (API, VSOCK):
            (self.run_dir / name).unlink(missing_ok=True)
        log = open(self.run_dir / "console.log", "ab")
        proc = subprocess.Popen(
            [self.binary, "--api-sock", API, "--level", "Warning"],
            cwd=self.run_dir,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,  # survives the trial worker's signals
        )
        log.close()
        self.pid_file.write_text(str(proc.pid))
        deadline = time.monotonic() + 10
        while not (self.run_dir / API).exists():
            if proc.poll() is not None or time.monotonic() > deadline:
                raise VmmError(
                    f"Firecracker did not start; see {self.run_dir}/console.log"
                )
            time.sleep(0.01)

    def api(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float = 600,
    ) -> dict[str, Any]:
        conn = _UnixHTTP(str(self.run_dir / API), timeout)
        try:
            conn.request(
                method,
                path,
                json.dumps(body) if body is not None else None,
                {"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            data = resp.read().decode()
        finally:
            conn.close()
        if resp.status >= 300:
            raise VmmError(f"Firecracker {method} {path}: {resp.status} {data}")
        return json.loads(data) if data else {}

    def boot(
        self,
        *,
        kernel: str,
        initrd: str | None,
        boot_args: str,
        cpus: int,
        memory_mib: int,
        huge_pages: bool,
        free_page_reporting: bool = False,
    ) -> None:
        machine: dict[str, Any] = {"vcpu_count": cpus, "mem_size_mib": memory_mib}
        if huge_pages:
            machine["huge_pages"] = "2M"
        self.api("PUT", "/machine-config", machine)
        source: dict[str, Any] = {"kernel_image_path": kernel, "boot_args": boot_args}
        if initrd:
            source["initrd_path"] = initrd
        self.api("PUT", "/boot-source", source)
        self.api(
            "PUT",
            "/drives/rootfs",
            {
                "drive_id": "rootfs",
                "path_on_host": DISK,
                "is_root_device": False,
                "is_read_only": False,
            },
        )
        self.api("PUT", "/vsock", {"guest_cid": GUEST_CID, "uds_path": VSOCK})
        if free_page_reporting:
            # The guest reports free memory, which Firecracker drops: those pages
            # are zero in a snapshot and become holes in its memory file.
            self.api(
                "PUT",
                "/balloon",
                {"amount_mib": 0, "deflate_on_oom": False, "free_page_reporting": True},
            )
        self.api("PUT", "/actions", {"action_type": "InstanceStart"})

    def snapshot(self, state: str, memory: str) -> None:
        """Pause and write a full snapshot (paths relative to the run dir)."""
        self.api("PATCH", "/vm", {"state": "Paused"})
        self.api(
            "PUT",
            "/snapshot/create",
            {"snapshot_type": "Full", "snapshot_path": state, "mem_file_path": memory},
        )

    def load(self, *, state: Path, memory: Path, uffd_socket: Path | None) -> None:
        backend = (
            {"backend_type": "Uffd", "backend_path": str(uffd_socket)}
            if uffd_socket
            else {"backend_type": "File", "backend_path": str(memory)}
        )
        self.api(
            "PUT",
            "/snapshot/load",
            {"snapshot_path": str(state), "mem_backend": backend, "resume_vm": True},
        )

    def kill(self) -> None:
        kill_pid_file(self.pid_file)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_pid_file(path: Path) -> None:
    try:
        pid = int(path.read_text())
    except (OSError, ValueError):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    for _ in range(100):
        if not pid_alive(pid):
            return
        time.sleep(0.02)


def connect_vsock(uds: Path, port: int, *, timeout: float = 10.0) -> socket.socket:
    """Return a stream connected to guest ``port`` through Firecracker's vsock."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(str(uds))
        sock.sendall(f"CONNECT {port}\n".encode())
        line = b""
        while not line.endswith(b"\n"):
            chunk = sock.recv(1)
            if not chunk:
                raise ConnectionError(f"vsock {uds}:{port} closed during handshake")
            line += chunk
        if not line.startswith(b"OK"):
            raise ConnectionError(f"vsock {uds}:{port} refused: {line!r}")
        sock.settimeout(None)
    except BaseException:
        sock.close()
        raise
    return sock


def http_ok(uds: Path, port: int, path: str = "/health") -> bool:
    try:
        with connect_vsock(uds, port, timeout=3.0) as sock:
            sock.settimeout(3.0)
            sock.sendall(f"GET {path} HTTP/1.0\r\n\r\n".encode())
            return sock.recv(16).startswith(b"HTTP/1.1 200")
    except OSError:
        return False


def wait_http_ok(uds: Path, port: int, *, timeout: float = 300.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if http_ok(uds, port):
            return
        time.sleep(0.05)
    raise TimeoutError(f"no HTTP 200 on vsock {uds}:{port} within {timeout:.0f}s")


def guest_exec(
    uds: Path, script: str, *, stdin: bytes = b"", timeout: float = 1800
) -> str:
    """Run ``script`` with bash as root in the guest; return its output.

    The base image's ``nika-guest-exec`` reads a base64 line (the script), a
    byte-count line and that many bytes of stdin, and ends the output with the
    exit status. The stdin length is sent up front because Firecracker's vsock
    drops a half-closed connection.
    """
    sock = connect_vsock(uds, EXEC_PORT)
    try:
        sock.settimeout(timeout)
        sock.sendall(
            base64.b64encode(script.encode()) + f"\n{len(stdin)}\n".encode() + stdin
        )
        out = bytearray()
        while chunk := sock.recv(1 << 16):
            out += chunk
    finally:
        sock.close()
    body, sep, tail = bytes(out).rpartition(EXIT_MARK)
    status = tail.split(b"\n", 1)[
        0
    ].strip()  # late output of background jobs may follow
    if not sep or not status.isdigit():
        raise VmmError(f"guest command ended without a status: {script[:80]!r}")
    if int(status) != 0:
        raise VmmError(
            f"guest command failed ({status.decode()}): {script[:80]!r}\n"
            f"{body.decode(errors='replace')[-2000:]}"
        )
    return body.decode(errors="replace")


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while data := src.recv(1 << 16):
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
    """Forward a local TCP port to guest ``port`` until closed."""

    def __init__(self, uds: Path, port: int, *, host: str = "127.0.0.1") -> None:
        self._uds = uds
        self._port = port
        self._server = socket.create_server((host, 0))
        self.port: int = self._server.getsockname()[1]
        self._closed = threading.Event()
        threading.Thread(
            target=self._serve, name=f"vsock-fwd-{port}", daemon=True
        ).start()

    def _serve(self) -> None:
        while not self._closed.is_set():
            try:
                client, _ = self._server.accept()
            except OSError:
                return
            try:
                upstream = connect_vsock(self._uds, self._port)
            except OSError:
                client.close()
                continue
            for a, b in ((client, upstream), (upstream, client)):
                threading.Thread(target=_pipe, args=(a, b), daemon=True).start()

    def close(self) -> None:
        self._closed.set()
        try:
            self._server.close()
        except OSError:
            pass


class HostProxy:
    """HTTP CONNECT proxy the guest reaches over vsock (guest -> host, ``port``).

    Firecracker connects a guest's connection to host CID 2 ``port`` to the
    Unix socket ``<vsock.sock>_<port>`` in the VM's directory. Used while
    building golden VMs, which have no network of their own.
    """

    def __init__(self, run_dir: Path, port: int) -> None:
        self.port = port
        self._path = run_dir / f"{VSOCK}_{port}"
        self._path.unlink(missing_ok=True)
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self._path))
        self._server.listen(64)
        threading.Thread(target=self._serve, name="vsock-proxy", daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                client, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._tunnel, args=(client,), daemon=True).start()

    @staticmethod
    def _tunnel(client: socket.socket) -> None:
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(4096)
                if not chunk:
                    return
                head += chunk
            request, _, rest = head.partition(b"\r\n\r\n")
            method, target, _ = request.split(b"\r\n", 1)[0].decode().split(" ", 2)
            if method != "CONNECT":
                client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                return
            host, _, port = target.rpartition(":")
            upstream = socket.create_connection((host, int(port)), timeout=30)
            upstream.settimeout(None)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if rest:
                upstream.sendall(rest)
        except (OSError, ValueError):
            client.close()
            return
        for a, b in ((client, upstream), (upstream, client)):
            threading.Thread(target=_pipe, args=(a, b), daemon=True).start()

    def guest_env(self) -> str:
        """Shell prefix that bridges the guest's 127.0.0.1:<port> to the proxy."""
        return (
            f"socat TCP-LISTEN:{self.port},bind=127.0.0.1,fork,reuseaddr "
            f"VSOCK-CONNECT:2:{self.port} 2>/dev/null & proxy=$!; "
            "trap 'kill $proxy; wait $proxy' EXIT; "
            f"export HTTPS_PROXY=http://127.0.0.1:{self.port} "
            f"HTTP_PROXY=http://127.0.0.1:{self.port}; "
        )

    def close(self) -> None:
        try:
            self._server.close()
        finally:
            self._path.unlink(missing_ok=True)
