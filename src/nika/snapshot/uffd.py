"""Userfaultfd page server for restoring snapshots onto huge pages.

  python -m nika.snapshot.uffd <socket> <memory_file>

Firecracker restores 2 MiB huge-page memory only through a page server: it
connects to ``socket`` and passes the userfaultfd plus the guest memory
layout. This server answers page faults from ``memory_file`` and, in the
background, copies the file's data into guest memory, so the guest stops
faulting shortly after it resumes. Holes in the (sparse) file are skipped.
"""

from __future__ import annotations

import array
import ctypes
import errno
import fcntl
import json
import mmap
import os
import select
import socket
import struct
import sys
import threading

UFFDIO_COPY = 0xC028AA03  # _IOWR(0xAA, 0x03, struct uffdio_copy)
UFFD_EVENT_PAGEFAULT = 0x12
CHUNK = 64 << 20


def _copy(fd: int, dst: int, src: int, length: int) -> int:
    """UFFDIO_COPY; return bytes copied (0 when the first page already exists)."""
    buf = bytearray(struct.pack("QQQQq", dst, src, length, 0, 0))
    try:
        fcntl.ioctl(fd, UFFDIO_COPY, buf, True)
    except OSError as exc:
        if exc.errno not in (errno.EEXIST, errno.EAGAIN):
            raise
    return max(struct.unpack_from("q", buf, 32)[0], 0)


def _data_extents(fd: int):
    """Yield ``(start, end)`` byte ranges of ``fd`` that hold data (not holes)."""
    size = os.fstat(fd).st_size
    pos = 0
    while pos < size:
        try:
            start = os.lseek(fd, pos, os.SEEK_DATA)
        except OSError:  # ENXIO: only a hole remains
            return
        end = os.lseek(fd, start, os.SEEK_HOLE)
        yield start, end
        pos = end


def serve(socket_path: str, memory_file: str) -> None:
    if os.path.exists(socket_path):
        os.unlink(socket_path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(socket_path)
    server.listen(1)
    conn, _ = server.accept()
    fds = array.array("i")
    msg, ancillary, _, _ = conn.recvmsg(4096, socket.CMSG_LEN(4))
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            fds.frombytes(data[:4])
    uffd = fds[0]
    regions = json.loads(msg)
    with open(memory_file, "rb") as fh:
        source = mmap.mmap(
            fh.fileno(),
            0,
            flags=mmap.MAP_PRIVATE,
            prot=mmap.PROT_READ | mmap.PROT_WRITE,
        )
        extents = list(_data_extents(fh.fileno()))
    base = ctypes.addressof(ctypes.c_char.from_buffer(source))

    def fault(addr: int) -> None:
        for region in regions:
            start = region["base_host_virt_addr"]
            if start <= addr < start + region["size"]:
                size = region["page_size"]
                page = addr & ~(size - 1)
                _copy(uffd, page, base + region["offset"] + page - start, size)
                return

    def prefetch() -> None:
        # Only the file's data: holes are memory the guest never used (or
        # reported free); those pages are served on first access, if ever.
        for region in regions:
            size, offset = region["page_size"], region["offset"]
            for data_start, data_end in extents:
                lo = max(data_start, offset) - offset
                hi = min(data_end, offset + region["size"]) - offset
                if lo >= hi:
                    continue
                pos, end = lo & ~(size - 1), (hi + size - 1) & ~(size - 1)
                while pos < end:
                    n = min(CHUNK, end - pos)
                    done = _copy(
                        uffd,
                        region["base_host_virt_addr"] + pos,
                        base + offset + pos,
                        n,
                    )
                    pos += done or size  # a fault filled that page first

    threading.Thread(target=prefetch, daemon=True).start()
    poller = select.poll()
    poller.register(uffd, select.POLLIN)
    while True:
        poller.poll()
        try:
            event = os.read(uffd, 32)
        except BlockingIOError:
            continue
        except OSError:
            return
        if not event:
            return  # Firecracker exited
        if event[0] == UFFD_EVENT_PAGEFAULT:
            fault(struct.unpack_from("Q", event, 16)[0])


if __name__ == "__main__":
    serve(sys.argv[1], sys.argv[2])
