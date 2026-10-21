"""Userfaultfd page server for restoring snapshots onto huge pages.

  python -m nika.firecracker.uffd <socket> <memory_file> [<memory_diff>]

Firecracker restores 2 MiB huge-page memory only through a page server: it
connects to ``socket`` and passes the userfaultfd plus the guest memory
layout. This server answers page faults from ``memory_file`` and, in the
background, copies the file's data into guest memory, so the guest stops
faulting shortly after it resumes. Holes in the (sparse) file are skipped.

With ``memory_diff`` (a case image, see ``nika.firecracker.memdiff``), the
server first reads ``memory_file`` sequentially and writes the diff's pages
into its private mapping, so the guest sees the case's memory.
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
import time

UFFDIO_COPY = 0xC028AA03  # _IOWR(0xAA, 0x03, struct uffdio_copy)
UFFD_EVENT_PAGEFAULT = 0x12
CHUNK = 64 << 20
# How long to wait for free huge pages before giving up on a page.
ENOMEM_WAIT_SEC = 600


def _copy(fd: int, dst: int, src: int, length: int) -> int:
    """UFFDIO_COPY; return bytes copied (0 when the first page already exists).

    ENOMEM means the huge-page pool is empty for now. Exiting would freeze the
    guest on its next fault for good, so wait for other VMs to release pages.
    """
    deadline = None
    while True:
        buf = bytearray(struct.pack("QQQQq", dst, src, length, 0, 0))
        try:
            fcntl.ioctl(fd, UFFDIO_COPY, buf, True)
        except OSError as exc:
            if exc.errno == errno.ENOMEM:
                copied = struct.unpack_from("q", buf, 32)[0]
                if copied > 0:
                    return copied  # the caller continues after these pages
                if deadline is None:
                    deadline = time.monotonic() + ENOMEM_WAIT_SEC
                    print("huge pages exhausted; waiting", file=sys.stderr, flush=True)
                if time.monotonic() < deadline:
                    time.sleep(0.5)
                    continue
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


def _read_ahead(fd: int, extents: list[tuple[int, int]]) -> None:
    """Read ``extents`` sequentially into the page cache.

    Writing diff pages into the private mapping faults each base page in first;
    from a cold cache those are random reads, many times slower than this.
    """
    for start, end in extents:
        os.posix_fadvise(fd, start, end - start, os.POSIX_FADV_SEQUENTIAL)
        pos = start
        while pos < end:
            n = len(os.pread(fd, min(CHUNK, end - pos), pos))
            if not n:
                break
            pos += n


def _with_diff_blocks(
    extents: list[tuple[int, int]], diff_pages: list[int]
) -> list[tuple[int, int]]:
    """``extents`` plus the 2 MiB blocks holding diff pages, merged."""
    block = 2 << 20
    ranges = sorted(
        extents
        + [
            (p * 4096 // block * block, (p * 4096 // block + 1) * block)
            for p in diff_pages
        ]
    )
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def serve(socket_path: str, memory_file: str, memory_diff: str | None = None) -> None:
    if os.path.exists(socket_path):
        os.unlink(socket_path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    from nika.firecracker.vmm import unix_path

    with unix_path(socket_path) as name:
        server.bind(name)
    server.listen(1)
    # Prepare memory before accepting: Firecracker waits on the connection.
    with open(memory_file, "rb") as fh:
        source = mmap.mmap(
            fh.fileno(),
            0,
            flags=mmap.MAP_PRIVATE,
            prot=mmap.PROT_READ | mmap.PROT_WRITE,
        )
        extents = list(_data_extents(fh.fileno()))
        if memory_diff:
            from nika.firecracker import memdiff

            _read_ahead(fh.fileno(), extents)
            memdiff.apply(source, memory_diff)
            extents = _with_diff_blocks(extents, memdiff.read_index(memory_diff))
    conn, _ = server.accept()
    fds = array.array("i")
    msg, ancillary, _, _ = conn.recvmsg(4096, socket.CMSG_LEN(4))
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            fds.frombytes(data[:4])
    uffd = fds[0]
    regions = json.loads(msg)
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
    serve(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
