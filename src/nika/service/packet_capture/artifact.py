"""Transfer capture artifacts from lab nodes to the session directory."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

from nika.runtime.base import LabRuntime


def capture_dir(session_dir: str, capture_id: str) -> Path:
    return Path(session_dir) / "packet_captures" / capture_id


def meta_path(session_dir: str, capture_id: str) -> Path:
    return capture_dir(session_dir, capture_id) / "meta.json"


def artifact_file_path(session_dir: str, capture_id: str) -> Path:
    return capture_dir(session_dir, capture_id) / "capture.pcapng"


def write_meta(session_dir: str, meta: dict) -> None:
    path = meta_path(session_dir, str(meta["capture_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_meta(session_dir: str, capture_id: str) -> dict:
    path = meta_path(session_dir, capture_id)
    if not path.is_file():
        raise FileNotFoundError(f"Capture {capture_id!r} not found")
    return json.loads(path.read_text(encoding="utf-8"))


def pull_remote_file(runtime: LabRuntime, device: str, remote_path: str) -> bytes:
    container = runtime.get_container(device)
    stream, _stat = container.get_archive(remote_path)
    buf = io.BytesIO()
    for chunk in stream:
        buf.write(chunk)
    buf.seek(0)
    with tarfile.open(fileobj=buf, mode="r") as tar:
        member = tar.next()
        if member is None:
            raise RuntimeError(f"empty archive for {device}:{remote_path}")
        handle = tar.extractfile(member)
        if handle is None:
            raise RuntimeError(f"missing file {device}:{remote_path}")
        return handle.read()


def store_artifact(
    runtime: LabRuntime,
    *,
    session_dir: str,
    capture_id: str,
    device: str,
    remote_path: str,
) -> tuple[Path, str]:
    """Copy remote capture file into the session artifact directory."""
    content = pull_remote_file(runtime, device, remote_path)
    dest = artifact_file_path(session_dir, capture_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    return dest, digest
