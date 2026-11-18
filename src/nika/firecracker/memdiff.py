"""Compressed page diffs between two guest memory files.

A case image stores only the 4 KiB pages of its memory that differ from the
lab image it was taken from, zstd-compressed:

  line 1    JSON header: {"version", "page_size", "pages", "index_bytes", "frames"}
  then      page numbers (little-endian uint64, ascending), ``index_bytes`` long
  then      zstd frames of page data, ``FRAME_PAGES`` pages each

Pages are compared in 2 MiB blocks first, so unchanged memory costs one compare.
"""

from __future__ import annotations

import json
import mmap
import os
import struct
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterator

import zstandard

PAGE = 4096
BLOCK = 2 << 20
FRAME_PAGES = 1024  # 4 MiB of page data per zstd frame
READ = 64 << 20
VERSION = 1


def _changed_pages(base: Path, case: Path, start: int, length: int) -> list[int]:
    """Page numbers in ``[start, start + length)`` where ``case`` differs from ``base``."""
    pages = []
    with open(base, "rb") as fb, open(case, "rb") as fc:
        a = os.pread(fb.fileno(), length, start).ljust(length, b"\0")
        b = os.pread(fc.fileno(), length, start).ljust(length, b"\0")
    if a == b:
        return pages
    for block in range(0, length, BLOCK):
        if a[block : block + BLOCK] == b[block : block + BLOCK]:
            continue
        for off in range(block, min(block + BLOCK, length), PAGE):
            if a[off : off + PAGE] != b[off : off + PAGE]:
                pages.append((start + off) // PAGE)
    return pages


def encode(base: Path, case: Path, out: Path) -> dict[str, int]:
    """Write the diff of ``case`` against ``base`` to ``out``; return its sizes."""
    size = os.stat(case).st_size
    chunks = range(0, size, READ)
    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
        index = [
            page
            for part in pool.map(
                lambda s: _changed_pages(base, case, s, min(READ, size - s)), chunks
            )
            for page in part
        ]
    cctx = zstandard.ZstdCompressor(level=3)
    frames = []
    with open(case, "rb") as fc:
        mm = mmap.mmap(fc.fileno(), 0, prot=mmap.PROT_READ) if size else b""
        for i in range(0, len(index), FRAME_PAGES):
            data = b"".join(
                mm[p * PAGE : p * PAGE + PAGE] for p in index[i : i + FRAME_PAGES]
            )
            frames.append(cctx.compress(data))
    raw_index = struct.pack(f"<{len(index)}Q", *index)
    header = {
        "version": VERSION,
        "page_size": PAGE,
        "pages": len(index),
        "index_bytes": len(raw_index),
        "frames": [len(f) for f in frames],
    }
    tmp = out.with_name(out.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write((json.dumps(header) + "\n").encode())
        fh.write(raw_index)
        for frame in frames:
            fh.write(frame)
    os.replace(tmp, out)
    return {"pages": len(index), "bytes": out.stat().st_size}


def read_index(path: Path) -> list[int]:
    """Page numbers a diff holds."""
    with open(path, "rb") as fh:
        header = json.loads(fh.readline())
        raw = fh.read(header["index_bytes"])
    return list(struct.unpack(f"<{header['pages']}Q", raw))


def pages(path: Path) -> Iterator[tuple[int, bytes]]:
    """Yield ``(page_number, data)`` for every page in a diff."""
    dctx = zstandard.ZstdDecompressor()
    with open(path, "rb") as fh:
        header = json.loads(fh.readline())
        if header.get("version") != VERSION or header.get("page_size") != PAGE:
            raise ValueError(f"unsupported memory diff {path}: {header}")
        raw = fh.read(header["index_bytes"])
        index = struct.unpack(f"<{header['pages']}Q", raw)
        pos = 0
        for length in header["frames"]:
            data = dctx.decompress(fh.read(length))
            for i in range(len(data) // PAGE):
                yield index[pos + i], data[i * PAGE : (i + 1) * PAGE]
            pos += len(data) // PAGE


def apply(memory: mmap.mmap, path: Path) -> int:
    """Write a diff's pages into a writable mapping of the base file; return pages."""
    count = 0
    for page, data in pages(path):
        memory[page * PAGE : page * PAGE + PAGE] = data
        count += 1
    return count
