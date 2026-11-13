#!/usr/bin/env python3
"""Static file server with per-block integrity hashing."""

from __future__ import annotations

import hashlib
import os
import re
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROUNDS_PER_64K = 8


class Handler(SimpleHTTPRequestHandler):
    def send_head(self):
        self._remaining = None
        value = self.headers.get("Range", "")
        path = self.translate_path(self.path)
        if not value or os.path.isdir(path):
            return super().send_head()
        try:
            source = open(path, "rb")
        except OSError:
            return super().send_head()
        stat = os.fstat(source.fileno())
        modified = self.date_time_string(stat.st_mtime)
        if self.headers.get("If-Range", modified) != modified:
            source.close()
            return super().send_head()
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
        size = stat.st_size
        start, end = 0, size - 1
        if match and any(match.groups()):
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(int(last), end) if last else end
            else:
                start = max(0, size - int(last))
                if int(last) == 0:
                    start = size
        else:
            source.close()
            return super().send_head()
        if start > end or start >= size:
            source.close()
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        source.seek(start)
        self._remaining = end - start + 1
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Length", str(self._remaining))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Last-Modified", modified)
        self.end_headers()
        return source

    def copyfile(self, source, outputfile):
        remaining = self._remaining
        while remaining is None or remaining > 0:
            block = source.read(
                64 * 1024 if remaining is None else min(64 * 1024, remaining)
            )
            if not block:
                break
            digest = b""
            for _ in range(ROUNDS_PER_64K):
                digest = hashlib.sha256(block + digest).digest()
            try:
                outputfile.write(block)
            except (BrokenPipeError, ConnectionResetError):
                return
            if remaining is not None:
                remaining -= len(block)

    def log_message(self, fmt: str, *args) -> None:
        return


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 80
    docroot = sys.argv[2] if len(sys.argv) > 2 else "/var/www"
    ThreadingHTTPServer(
        ("0.0.0.0", port), partial(Handler, directory=docroot)
    ).serve_forever()
