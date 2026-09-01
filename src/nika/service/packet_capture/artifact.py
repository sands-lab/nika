"""Packet capture metadata stored on lab nodes (never under session/result dirs)."""

from __future__ import annotations

import json
import shlex

from nika.runtime.base import LabRuntime


def remote_meta_path(capture_id: str) -> str:
    return f"/tmp/nika-capture-{capture_id}.meta.json"


def write_meta(runtime: LabRuntime, device: str, meta: dict) -> None:
    path = remote_meta_path(str(meta["capture_id"]))
    content = json.dumps(meta, indent=2, sort_keys=True) + "\n"
    runtime.write_file(device, path, content)


def read_meta(
    runtime: LabRuntime,
    capture_id: str,
    *,
    device: str | None = None,
) -> dict:
    path = remote_meta_path(capture_id)
    quoted = shlex.quote(path)
    nodes = [device] if device else runtime.list_nodes()
    for node in nodes:
        exists = runtime.exec(
            node,
            f"test -f {quoted} && echo yes || echo no",
            timeout=5,
        ).strip()
        if exists != "yes":
            continue
        raw = runtime.exec(node, f"cat {quoted}", timeout=10)
        return json.loads(raw)
    raise FileNotFoundError(f"Capture {capture_id!r} not found on lab nodes")
