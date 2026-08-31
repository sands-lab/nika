"""Shared helpers for benchmark trial tests."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

ROW_A = {
    "scenario": "dc_clos",
    "problem": "link_down",
    "topo_size": "s",
    "inject": {"host_name": "client_0", "intf_name": "eth0"},
}
ROW_B = {
    "scenario": "dc_clos",
    "problem": "link_flap",
    "topo_size": "s",
    "inject": {"host_name": "client_0", "intf_name": "eth0"},
}


def write_valid_trial(
    path: Path,
    *,
    outcome: str = "success",
    session_id: str | None = None,
    fingerprint: str | None = None,
) -> None:
    path.mkdir(parents=True, exist_ok=True)
    sid = session_id or path.name
    run_meta = {
        "session_id": sid,
        "status": "finished",
        "outcome": outcome,
        "benchmark_fingerprint": fingerprint or "fp",
    }
    (path / "run.json").write_text(json.dumps(run_meta), encoding="utf-8")
    (path / "ground_truth.json").write_text("{}", encoding="utf-8")
    (path / "messages.jsonl").write_text("", encoding="utf-8")
    (path / "eval_metrics.json").write_text("{}", encoding="utf-8")
    if outcome == "success":
        (path / "submission.json").write_text("{}", encoding="utf-8")


def mini_cases_yaml(path: Path, rows: list[dict] | None = None) -> Path:
    payload = {"seed": 42, "cases": rows or [ROW_A, ROW_B]}
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path
