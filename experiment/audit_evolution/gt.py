"""Ground truth: every production KB record, with the trials that show it where known.

Trials come from, in order of preference:
  signature  the record's signature fires (``<ROOT>/gt/match/<run>.json``, written by
             ``nika.audit.cli run --kb <production kb>``);
  scope      the record's scope (scenarios, fault types, healthy, agent types) selects
             trials of runs whose commit predates the record's fix;
  run        neither applies: the record counts per run and is matched by meaning.
Writes ``<ROOT>/gt/gt.json``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from functools import cache
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

PRODUCTION_KB = Path("/workspace/nika/src/nika/audit/kb_data/issues")
GIT = Path("/workspace/nika")


@cache
def fixed_before(fix: str, commit: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(GIT), "merge-base", "--is-ancestor", fix, commit],
            capture_output=True,
        ).returncode
        == 0
    )


def trial_info(bench: str) -> dict[str, dict[str, dict]]:
    info: dict[str, dict[str, dict]] = {}
    for run in C.runs(bench):
        for rj in (C.benchmark_dir(bench) / run / "trials").glob("*/run.json"):
            d = json.loads(rj.read_text())
            info.setdefault(run, {})[rj.parent.name] = {
                "scenario": d.get("scenario_name"),
                "faults": set(d.get("problem_names") or []) - {"healthy"},
                "agent": d.get("agent_type"),
                "commit": d.get("nika_git_commit"),
            }
    return info


def scoped(
    record: dict, info: dict[str, dict[str, dict]]
) -> dict[str, list[str]] | None:
    scope = record.get("scope") or {}
    keys = {"scenarios", "fault_types", "healthy", "agent_types"}
    if not keys & set(scope):
        return None
    out: dict[str, list[str]] = {}
    for run, trials in info.items():
        hit = []
        for tid, t in trials.items():
            if scope.get("scenarios") and t["scenario"] not in scope["scenarios"]:
                continue
            if scope.get("fault_types") and not t["faults"] & set(scope["fault_types"]):
                continue
            if "healthy" in scope and bool(scope["healthy"]) != (not t["faults"]):
                continue
            if scope.get("agent_types") and t["agent"] not in scope["agent_types"]:
                continue
            fix = record.get("fixed_in")
            if fix and t["commit"] and fixed_before(fix, t["commit"]):
                continue
            hit.append(tid)
        if hit:
            out[run] = sorted(hit)
    return out


def main(bench: str = "nika-0.2.0") -> None:
    records = {}
    for f in PRODUCTION_KB.glob("*.yaml"):
        d = yaml.safe_load(f.read_text())
        records[d["id"]] = d
    fired: dict[str, dict[str, list[str]]] = {}
    for f in sorted((C.ROOT / "gt" / "match").glob("*.json")):
        report = json.loads(f.read_text())
        for hit in report["known_issues"] + report["leads"]:
            if hit["id"] in records:
                fired.setdefault(hit["id"], {})[f.stem] = sorted(hit["trials"])
    info = trial_info(bench)
    gt = {}
    for rid, d in sorted(records.items()):
        entry = {
            "class": d["class"],
            "title": d["title"],
            "summary": d.get("summary", ""),
            "status": d.get("status"),
        }
        if rid in fired:
            gt[rid] = entry | {"source": "signature", "trials": fired[rid]}
        elif (s := scoped(d, info)) is not None:
            gt[rid] = entry | {"source": "scope", "trials": s}
        else:
            gt[rid] = entry | {"source": "run", "trials": None}
    C.write_json(C.ROOT / "gt" / "gt.json", gt)
    counts = {}
    for e in gt.values():
        counts[e["source"]] = counts.get(e["source"], 0) + 1
    print(
        len(gt),
        counts,
        "scoped with no trials:",
        [k for k, e in gt.items() if e["trials"] == {}],
    )


if __name__ == "__main__":
    main(*sys.argv[1:])
