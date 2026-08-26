"""Resume support for benchmark batch runs by scanning session artifacts."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from nika.config import SESSIONS_DIR
from nika.utils.session_index import SessionIndex
from nika.workflows.session.close import close_session


def benchmark_row_fingerprint(row: dict[str, Any]) -> str:
    payload = {
        "scenario": row["scenario"],
        "problem": row["problem"],
        "topo_size": row.get("topo_size") or "",
        "topo": row.get("topo") or "",
        "igp": row.get("igp") or "",
        "bgp_mode": row.get("bgp_mode") or "",
        "rpki": bool(row.get("rpki", False)),
        "inject": {
            str(k): str(v) for k, v in sorted((row.get("inject") or {}).items())
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def benchmark_row_from_case(
    *,
    scenario: str,
    problem: str,
    topo_size: str,
    inject_params: dict[str, str],
    topo: str | None = None,
    igp: str | None = None,
    bgp_mode: str | None = None,
    rpki: bool | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "scenario": scenario,
        "problem": problem,
        "topo_size": topo_size or "",
        "inject": dict(inject_params),
    }
    if topo:
        row["topo"] = topo
    if igp:
        row["igp"] = igp
    if bgp_mode:
        row["bgp_mode"] = bgp_mode
    if rpki is not None:
        row["rpki"] = bool(rpki)
    return row


def cleanup_benchmark_session(
    session_id: str | None,
    session_dir: str | Path | None,
) -> None:
    """Remove a partial or failed benchmark session and any runtime state."""
    if session_id:
        try:
            close_session(
                session_id=session_id,
                undeploy=True,
                session_dir=session_dir,
            )
        except (FileNotFoundError, ValueError):
            pass
        SessionIndex().purge(session_id)
        runtime_path = Path(SESSIONS_DIR) / f"{session_id}.json"
        if runtime_path.exists():
            runtime_path.unlink()

    if session_dir:
        path = Path(session_dir)
        if path.exists():
            shutil.rmtree(path)
