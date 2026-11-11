"""Splice a running Kathara VDE hub into a controller-owned bridge.

Runs inside the controller's helper container (host PID namespace,
privileged), never in a lab node. It is shipped as ``python3 -c`` source, so
it uses the standard library only.

Kathara's VDE network plugin gives every collision domain one ``vde_switch``
hub. Each lab interface is a TAP inside the node; the plugin relays its frames
over a connected datagram socket to one hub port. This script duplicates the
hub's two port sockets (``pidfd_getfd``), pauses the hub with ``SIGSTOP`` and
relays each port into a local TAP (``eth0`` toward the endpoint, ``eth1``
toward the peer) joined by ``br0``. ``tc`` on those TAPs then impairs the
wire while the lab interfaces, their ifindexes and counters stay untouched.
A second instance of the installed VDE binary forwards the relayed frames,
using the same port capacity as the original hub. This preserves the native
forwarding cost in both directions. ``SIGTERM`` resumes the hub and exits.
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import platform
import select
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid

# Injected with the shared standard-library source by vde_proxy.
_PROCESS_HELPERS = globals().get("_PROCESS_HELPERS", {})

_SYS_PIDFD_GETFD = 438
_SYS_PPOLL = {"x86_64": 271, "aarch64": 73}
_TUNSETIFF = 0x400454CA
_TUNGETIFF = 0x800454D2
_TUNGETDEVNETNS = 0x54E3
_IFF_TAP = 0x0002
_IFF_NO_PI = 0x1000
_BUF = 65536

_libc = ctypes.CDLL(None, use_errno=True)


def _steal(pid: int, fd: int) -> int:
    pidfd = os.pidfd_open(pid)
    try:
        stolen = _libc.syscall(_SYS_PIDFD_GETFD, pidfd, fd, 0)
        if stolen < 0:
            raise OSError(ctypes.get_errno(), f"pidfd_getfd({pid}, {fd})")
        return stolen
    finally:
        os.close(pidfd)


def _switch_pid(ctl: str) -> int:
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                argv = handle.read().split(b"\0")
        except OSError:
            continue
        if argv[0].endswith(b"vde_switch") and any(
            arg.endswith(b"/" + ctl.encode()) for arg in argv
        ):
            return int(entry)
    raise SystemExit(f"no vde_switch serves {ctl}")


def _unix_dgram_sockets(pid: int) -> list[tuple[int, int, int, int]]:
    """``(owner pid, fd, inode, peer inode)`` for connected datagram sockets."""
    output = subprocess.run(
        ["nsenter", "-t", str(pid), "-n", "ss", "-xpnH"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    rows = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 8 or fields[0] != "u_dgr" or fields[1] != "ESTAB":
            continue
        inode, peer = int(fields[5]), int(fields[7])
        users = line.partition("users:")[2]
        for chunk in users.split("pid=")[1:]:
            owner, _, rest = chunk.partition(",fd=")
            fd = rest.split(")")[0]
            rows.append((int(owner), int(fd), inode, peer))
    return rows


def _tap_fd_for(pid: int, data_fd: int) -> int:
    """Return the TAP fd a plugin thread relays to ``data_fd``.

    Each plugin thread blocks in ``ppoll`` on ``{datafd, tapfd, sigfd}``.
    """
    ppoll = _SYS_PPOLL[platform.machine()]
    for _attempt in range(200):
        for tid in os.listdir(f"/proc/{pid}/task"):
            try:
                with open(f"/proc/{pid}/task/{tid}/syscall") as handle:
                    fields = handle.read().split()
            except OSError:
                continue
            if len(fields) < 3 or not fields[0].isdigit() or int(fields[0]) != ppoll:
                continue
            if int(fields[2], 16) != 3:
                continue
            with open(f"/proc/{pid}/mem", "rb") as mem:
                mem.seek(int(fields[1], 16))
                pollfds = struct.unpack("ihhihhihh", mem.read(24))
            if pollfds[0] == data_fd:
                return pollfds[3]
        select.select([], [], [], 0.05)
    raise SystemExit(f"no VDE plugin thread relays fd {data_fd} of pid {pid}")


def _tap_identity(pid: int, tap_fd: int) -> tuple[int, str]:
    """Network namespace inode and interface name of a plugin TAP fd."""
    fd = _steal(pid, tap_fd)
    try:
        ifreq = bytearray(40)
        fcntl.ioctl(fd, _TUNGETIFF, ifreq)
        name = bytes(ifreq[:16]).split(b"\0")[0].decode()
        netns = fcntl.ioctl(fd, _TUNGETDEVNETNS)
        try:
            return os.fstat(netns).st_ino, name
        finally:
            os.close(netns)
    finally:
        os.close(fd)


def _open_tap(name: str) -> int:
    fd = os.open("/dev/net/tun", os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
    ifreq = struct.pack("16sH22x", name.encode(), _IFF_TAP | _IFF_NO_PI)
    fcntl.ioctl(fd, _TUNSETIFF, ifreq)
    return fd


def _ip(*args: str) -> None:
    subprocess.run(["ip", *args], check=True)


class _NativeForwarder:
    """Keep the installed VDE forwarding implementation in the impaired path."""

    def __init__(self, switch: int):
        self.namespace = os.open(f"/proc/{switch}/ns/mnt", os.O_RDONLY)
        self.directory = f"/tmp/nika-vde-{uuid.uuid4().hex}"
        native_directory = self.directory
        self.controls = []
        self.data = {}
        self.process = None
        try:
            self._run("mkdir", self.directory)
            binary = os.readlink(f"/proc/{switch}/exe")
            with open(f"/proc/{switch}/cmdline", "rb") as handle:
                argv = handle.read().split(b"\0")
            count = argv[argv.index(b"-n") + 1].decode() if b"-n" in argv else "32"
            self.process = subprocess.Popen(
                [
                    *self._prefix(),
                    binary,
                    "-n",
                    count,
                    "-x",
                    "--nostdin",
                    "-s",
                    native_directory + "/ctl",
                ],
                pass_fds=(self.namespace,),
                stdout=subprocess.DEVNULL,
            )
            root = f"/proc/{self.process.pid}/root"
            control_path = root + self.directory + "/ctl/ctl"
            deadline = time.monotonic() + 5
            while not os.path.exists(control_path):
                if self.process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Native VDE forwarder did not start")
                time.sleep(0.02)
            for name in ("eth0", "eth1"):
                data = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                self.data[name] = data
                data.bind(("\0nika-vde-" + uuid.uuid4().hex).encode().ljust(108, b"\0"))
                control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.controls.append(control)
                control.settimeout(5)
                control.connect(control_path)
                control.sendall(
                    struct.pack(
                        "=IIIH108s",
                        0xFEEDFACE,
                        3,
                        0,
                        socket.AF_UNIX,
                        data.getsockname(),
                    )
                    + b"nika\0"
                )
                response = b""
                while len(response) < 110:
                    part = control.recv(110 - len(response))
                    if not part:
                        raise RuntimeError("Native VDE port handshake ended early")
                    response += part
                peer = response[2:].split(b"\0", 1)[0].decode()
                if not peer.startswith(native_directory + "/ctl/"):
                    raise RuntimeError("Native VDE returned an unexpected port path")
                data.connect(root + peer)
                data.setblocking(False)
        except BaseException:
            self.close()
            raise

    def _prefix(self):
        return ["nsenter", f"--mount=/proc/self/fd/{self.namespace}", "--"]

    def _run(self, *args):
        return subprocess.run(
            [*self._prefix(), *args], pass_fds=(self.namespace,), check=True
        )

    def close(self):
        for data in self.data.values():
            data.close()
        for control in self.controls:
            control.close()
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        try:
            self._run("rmdir", self.directory)
        except (OSError, subprocess.CalledProcessError):
            pass
        if self.namespace is not None:
            os.close(self.namespace)
            self.namespace = None


def main() -> None:
    ctl, node_pid, ifname, mtu, ready = sys.argv[1:6]
    netns_ino = os.stat(f"/proc/{node_pid}/ns/net").st_ino
    switch = _switch_pid(ctl)
    switch_identity = _PROCESS_HELPERS["process_identity"](switch)
    switch_fd = _PROCESS_HELPERS["owned_pidfd"](switch_identity)
    if switch_fd is None or _switch_pid(ctl) != switch:
        raise SystemExit("VDE hub exited before its process identity was pinned")
    if os.stat("/proc/self/ns/net").st_ino == os.stat("/proc/1/ns/net").st_ino:
        raise SystemExit("Refusing a VDE splice in the host network namespace")
    sockets = _unix_dgram_sockets(switch)
    ports = [row for row in sockets if row[0] == switch]
    if len(ports) != 2:
        raise SystemExit(f"hub {ctl} has {len(ports)} ports, expected 2")
    owners = {inode: (pid, fd) for pid, fd, inode, _peer in sockets}
    endpoint_port = None
    for _pid, fd, _inode, peer in ports:
        plugin, data_fd = owners[peer]
        identity = _tap_identity(plugin, _tap_fd_for(plugin, data_fd))
        if identity == (netns_ino, ifname):
            endpoint_port = fd
    if endpoint_port is None:
        raise SystemExit(f"hub {ctl} has no port for {ifname}")
    peer_port = next(fd for _pid, fd, _i, _p in ports if fd != endpoint_port)

    taps = {"eth0": _open_tap("eth0"), "eth1": _open_tap("eth1")}
    _ip("link", "add", "br0", "type", "bridge", "stp_state", "0", "forward_delay", "0")
    for name in taps:
        _ip("link", "set", "dev", name, "mtu", mtu, "master", "br0", "up")
    _ip("link", "set", "dev", "br0", "up")

    wires = {
        "eth0": socket.socket(fileno=_steal(switch, endpoint_port)),
        "eth1": socket.socket(fileno=_steal(switch, peer_port)),
    }
    native = _NativeForwarder(switch)
    flags = socket.MSG_DONTWAIT
    route = {}
    for name, tap in taps.items():
        wire = wires[name]
        data = native.data[name]
        opposite = native.data["eth1" if name == "eth0" else "eth0"]
        route[tap] = (
            lambda tap=tap: os.read(tap, _BUF),
            lambda frame, opposite=opposite: opposite.send(frame, flags),
        )
        route[wire.fileno()] = (
            lambda wire=wire: wire.recv(_BUF, flags),
            lambda frame, tap=tap: os.write(tap, frame),
        )
        route[data.fileno()] = (
            lambda data=data: data.recv(_BUF, flags),
            lambda frame, wire=wire: wire.send(frame, flags),
        )

    def stop(*_args) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        with select.epoll() as poller:
            for fd in route:
                poller.register(fd, select.EPOLLIN)
            _PROCESS_HELPERS["send_pidfd"](switch_fd, signal.SIGSTOP)
            with open(ready + ".tmp", "w") as handle:
                import json

                json.dump(
                    {
                        "relay": _PROCESS_HELPERS["process_identity"](os.getpid()),
                        "hub": switch_identity,
                        "native": _PROCESS_HELPERS["process_identity"](
                            native.process.pid
                        ),
                        "directory": native.directory,
                    },
                    handle,
                )
            os.replace(ready + ".tmp", ready)
            while native.process.poll() is None:
                for fd, _event in poller.poll(1):
                    receive, forward = route[fd]
                    try:
                        forward(receive())
                    except OSError:
                        # A full peer queue or a TAP held down drops the frame,
                        # as a congested or dark wire would.
                        pass
    finally:
        try:
            _PROCESS_HELPERS["send_pidfd"](switch_fd, signal.SIGCONT)
        except ProcessLookupError:
            pass
        native.close()
        os.close(switch_fd)


if __name__ == "__main__":
    main()
