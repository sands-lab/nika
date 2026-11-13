"""Preserve configuration modification times across failure edits."""

from contextlib import contextmanager
import re
import shlex


@contextmanager
def preserve_mtime(runtime, node: str, path: str):
    """Restore a file's mtime after editing; ctime cannot be reset."""
    quoted = shlex.quote(path)
    epoch = runtime.exec(node, f"date -r {quoted} +%s.%N").strip()
    if not re.fullmatch(r"-?\d+\.\d{9}", epoch):
        raise RuntimeError(f"Cannot read modification time for {path} on {node}")
    try:
        yield
    finally:
        runtime.exec(node, f"touch -d @{epoch} -- {quoted}")
