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
``SIGTERM`` resumes the hub and exits.
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


def main() -> None:
    ctl, node_pid, ifname, mtu, ready = sys.argv[1:6]
    netns_ino = os.stat(f"/proc/{node_pid}/ns/net").st_ino
    switch = _switch_pid(ctl)
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
    flags = socket.MSG_DONTWAIT
    # fd -> (read one frame, forward it to the other side)
    route = {}
    for name, tap in taps.items():
        wire = wires[name]
        route[tap] = (
            lambda tap=tap: os.read(tap, _BUF),
            lambda frame, wire=wire: wire.send(frame, flags),
        )
        route[wire.fileno()] = (
            lambda wire=wire: wire.recv(_BUF, flags),
            lambda frame, tap=tap: os.write(tap, frame),
        )

    def resume(*_args) -> None:
        os.kill(switch, signal.SIGCONT)
        os._exit(0)

    signal.signal(signal.SIGTERM, resume)
    signal.signal(signal.SIGINT, resume)
    poller = select.epoll()
    for fd in route:
        poller.register(fd, select.EPOLLIN)
    os.kill(switch, signal.SIGSTOP)
    with open(ready, "w") as handle:
        handle.write(f"{os.getpid()} {switch}\n")
    while True:
        for fd, _event in poller.poll():
            receive, forward = route[fd]
            try:
                forward(receive())
            except OSError:
                # A full peer queue or a TAP held down drops the frame, as a
                # congested or dark wire would.
                pass


if __name__ == "__main__":
    main()
