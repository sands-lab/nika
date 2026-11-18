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
    """Extract a session artifact tarball into *dest_dir*.

    Files are overwritten, except ``*.jsonl`` logs that already exist locally:
    the agent host appends its own events (``agent_start``, the agent trace) to
    the same files, so remote lines it does not have yet are appended instead.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO(payload)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        logs, others = [], []
        for member in tar.getmembers():
            # Python 3.12+: the data filter rejects path traversal (CVE-2007-4559).
            member = tarfile.data_filter(member, str(dest))
            is_log = member.isfile() and member.name.endswith(".jsonl")
            (logs if is_log and (dest / member.name).is_file() else others).append(
                member
            )
        tar.extractall(dest, members=others, filter="data")
        for member in logs:
            remote = tar.extractfile(member).read().decode("utf-8")
            _append_missing_lines(dest / member.name, remote)
    return dest


def _append_missing_lines(path: Path, remote: str) -> None:
    local = path.read_text(encoding="utf-8")
    seen = set(local.splitlines())
    new = [line for line in remote.splitlines() if line.strip() and line not in seen]
    if not new:
        return
    with path.open("a", encoding="utf-8") as fh:
        if local and not local.endswith("\n"):
            fh.write("\n")
        fh.write("\n".join(new) + "\n")
