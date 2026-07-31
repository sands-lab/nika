"""Pack and unpack remote session artifact trees."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path


def pack_session_dir(session_dir: str | Path) -> bytes:
    """Return a gzip-compressed tar of *session_dir* (directory itself as ``.``)."""
    root = Path(session_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"Session directory not found: {root}")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(root, arcname=".")
    return buf.getvalue()


def unpack_session_dir(payload: bytes, dest_dir: str | Path) -> Path:
    """Extract a session artifact tarball into *dest_dir* (merged overwrite)."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO(payload)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        # Python 3.12+: filter= avoids CVE-2007-4559 style path traversal.
        tar.extractall(dest, filter="data")
    return dest
