"""Recall of the auditing agent against the reference issues.

    uv run python -m experiment.submission_audit.recall \\
        --runs /path/to/evolve-root --out recall.json

Each run directory (from ``replay.py``) holds one step per submission with
the issue memory before that step (``NN_<submission>/kb_before``), the final
memory (``kb``), and a mapping of every issue the run wrote to the reference
issues it describes (``map_issues.py`` writes ``mapping_llm.yaml``; the agent
runs also have a hand-checked ``mapping.yaml``).

A run *found* a reference issue once it wrote an issue mapped to it. Recall
after step *k* divides the reference issues found so far by those present in
the submissions audited so far (``present_in``).
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
_STEP = re.compile(r"^(\d\d)_(.+)$")


def load_reference(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def present(entry: dict, submission: str) -> bool:
    where = entry["present_in"]
    return where == "all" or submission in where


def _issue_ids(kb_dir: Path) -> set[str]:
    return {p.stem for p in (kb_dir / "issues").glob("*.yaml")}


def run_recall(
    run: Path, gt: list[dict], mapping_name: str = "mapping_llm.yaml"
) -> dict:
    mapping = yaml.safe_load((run / mapping_name).read_text(encoding="utf-8")) or {}
    steps = sorted(
        (m.group(1), m.group(2), p)
        for p in run.iterdir()
        if p.is_dir() and (m := _STEP.match(p.name))
    )
    initial = _issue_ids(steps[0][2] / "kb_before")
    snapshots = [s[2] / "kb_before" for s in steps[1:]] + [run / "kb"]
    gt_ids = {g["id"] for g in gt}
    seen: set[str] = set()
    curve = []
    found: set[str] = set()
    for (_, submission, _), snap in zip(steps, snapshots):
        seen |= {g["id"] for g in gt if present(g, submission)}
        learned = _issue_ids(snap) - initial
        found = {g for i in learned for g in mapping.get(i) or []} & gt_ids
        curve.append(
            {
                "submission": submission,
                "found": len(found),
                "present_so_far": len(seen),
                "recall_so_far": round(len(found & seen) / len(seen), 3),
            }
        )
    all_present = {g["id"] for g in gt if g["present_in"]}
    return {
        "run": run.name,
        "curve": curve,
        "found": sorted(found),
        "recall": round(len(found & all_present) / len(all_present), 3),
        "written": len(_issue_ids(run / "kb") - initial),
        "mapped": sum(1 for v in mapping.values() if v),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument(
        "--reference", type=Path, default=HERE / "reference_issues.yaml"
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--mapping", default="mapping_llm.yaml", help="Mapping file name in each run."
    )
    args = parser.parse_args()
    gt = load_reference(args.reference)
    runs = sorted(p for p in args.runs.iterdir() if (p / args.mapping).is_file())
    results = [run_recall(run, gt, args.mapping) for run in runs]
    union = set().union(*(set(r["found"]) for r in results))
    by_class: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for g in gt:
        by_class[g["class"]][1] += 1
        by_class[g["class"]][0] += g["id"] in union
    summary = {
        "reference_issues": len(gt),
        "runs": results,
        "union": sorted(union),
        "found_by_every_run": sorted(
            set.intersection(*(set(r["found"]) for r in results))
        ),
        "by_class": {c: {"found": f, "present": n} for c, (f, n) in by_class.items()},
        "missed_by_all": sorted({g["id"] for g in gt} - union),
    }
    args.out.write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    for r in results:
        n = len(r["curve"])
        points = [r["curve"][i]["found"] for i in (0, 4, 9, 14, n - 1) if i < n]
        print(
            f"{r['run']:9s} recall {r['recall']:.2f}, "
            f"found after 1/5/10/15/all: {points}"
        )
    both = set.intersection(*(set(r["found"]) for r in results))
    print(f"union of runs: {len(union)}/{len(gt)}; found by every run: {len(both)}")


if __name__ == "__main__":
    main()
