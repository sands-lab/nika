"""Session metadata for packet captures (pcap files stay on lab nodes)."""

from __future__ import annotations

import json
from pathlib import Path


def capture_dir(session_dir: str, capture_id: str) -> Path:
    return Path(session_dir) / "packet_captures" / capture_id


def meta_path(session_dir: str, capture_id: str) -> Path:
    return capture_dir(session_dir, capture_id) / "meta.json"


def write_meta(session_dir: str, meta: dict) -> None:
    path = meta_path(session_dir, str(meta["capture_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_meta(session_dir: str, capture_id: str) -> dict:
    path = meta_path(session_dir, capture_id)
    if not path.is_file():
        raise FileNotFoundError(f"Capture {capture_id!r} not found")
    return json.loads(path.read_text(encoding="utf-8"))
