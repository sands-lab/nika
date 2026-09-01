"""Resume support for benchmark batch runs by scanning session artifacts."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import quote

from nika.config import SESSIONS_DIR
from nika.utils.session_index import SessionIndex
from nika.workflows.session.close import close_session


from nika.workflows.benchmark.multi_fault import row_problems


def _fingerprint_inject(row: dict[str, Any]) -> dict[str, Any]:
    inject = row.get("inject") or {}
    problems = row_problems(row)
    if len(problems) > 1:
        nested: dict[str, dict[str, str]] = {}
        for problem in problems:
            piece = inject.get(problem) if isinstance(inject, dict) else {}
            if isinstance(piece, dict):
                nested[problem] = {str(k): str(v) for k, v in sorted(piece.items())}
        return nested
    if isinstance(inject, dict) and any(isinstance(v, dict) for v in inject.values()):
        return {
            str(k): str(v) for k, v in sorted(inject.items()) if not isinstance(v, dict)
        }
    return {str(k): str(v) for k, v in sorted(inject.items())}


def benchmark_row_identity(row: dict[str, Any]) -> dict[str, Any]:
    """Stable identity fields for one catalog/case row (names + deploy params)."""
    return {
        "scenario": row["scenario"],
        "problem": row["problem"],
        "problems": row_problems(row),
        "topo_size": row.get("topo_size") or "",
        "topo": row.get("topo") or "",
        "igp": row.get("igp") or "",
        "bgp_mode": row.get("bgp_mode") or "",
        "rpki": bool(row.get("rpki", False)),
        "backend": row.get("backend") or "",
        "device_profile": row.get("device_profile") or "",
        "inject": _fingerprint_inject(row),
    }


def benchmark_row_fingerprint(row: dict[str, Any]) -> str:
    """Canonical identity string for equality / Dev-Test isolation checks."""
    return json.dumps(
        benchmark_row_identity(row),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def benchmark_option_id(row: dict[str, Any]) -> str:
    """Return a readable, stable identity for one catalog option."""
    identity: list[tuple[str, Any]] = [
        ("scenario", row["scenario"]),
        ("topo_size", row.get("topo_size") or ""),
    ]
    identity.extend(
        (key, row[key])
        for key in ("topo", "igp", "bgp_mode", "rpki", "backend", "device_profile")
        if key in row
    )
    identity.append(("problem", row["problem"]))
    identity.extend(
        (f"inject.{key}", value)
        for key, value in sorted((row.get("inject") or {}).items())
    )

    def component(key: str, value: Any) -> str:
        if value in (None, ""):
            text = "-"
        elif isinstance(value, bool):
            text = str(value).lower()
        else:
            text = str(value)
        return f"{quote(key, safe='-._~')}={quote(text, safe='-._~')}"

    return "__".join(component(key, value) for key, value in identity)


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
    backend: str | None = None,
    device_profile: str | None = None,
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
    if backend:
        row["backend"] = backend
    if device_profile:
        row["device_profile"] = device_profile
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
