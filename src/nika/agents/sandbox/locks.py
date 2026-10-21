"""Host-wide locks shared by every NIKA process and thread."""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def host_file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive ``flock`` on *path* (created if missing)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
