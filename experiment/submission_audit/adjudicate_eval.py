"""Adjudication of scan groups against reviewer labels.

    uv run python -m experiment.submission_audit.adjudicate_eval \\
        --trajectories /path/to/trajectories/0.2.0 \\
        --base-url http://host:port/v1 --model Qwen3.8-27B-FP8 --mode on \\
        --out runs/adjudicate-on.json

``labels/scan_groups.yaml`` holds 120 scan groups that independent reviewers
judged. A group is real when its label is ``reference`` or ``new_real``.
Kept groups are those the adjudicator calls ``real`` (or also ``unclear``,
reported separately). Precision is the share of kept groups that are real;
recall is the share of real groups kept. The population estimate reweights
the two sampling strata (groups in two or more trials, groups in one) by how
many groups of each kind the submission had. The split is by submission;
change the prompt only on ``dev``.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import yaml

from experiment.submission_audit import skill  # noqa: F401  (adds auditlib to sys.path)

from auditlib.adjudicate import adjudicate_groups
from auditlib.kb import load_kb
from auditlib.scan import ScanConfig
from auditlib.trace import load_trial, trial_dirs

HERE = Path(__file__).resolve().parent
DEV = {
    "20261007_claude_code_qwen3_6_35b_a3b_fp8",
    "20261020_codex_qwen3_coder_next_fp8",
}


def _real(label: str) -> bool:
    return label in ("reference", "new_real")


def metrics(rows: list[dict], keep: set[str]) -> dict:
    kept = [r for r in rows if r["verdict"] in keep]
    real = [r for r in rows if _real(r["label"])]
    tp = sum(_real(r["label"]) for r in kept)
    out = {
        "groups": len(rows),
        "kept": len(kept),
        "precision": round(tp / len(kept), 3) if kept else None,
        "recall": round(tp / len(real), 3) if real else None,
    }
    # Population estimate: per submission and stratum, weight by group counts.
    num = den = 0.0
    for sub in {r["submission"] for r in rows}:
        mine = [r for r in rows if r["submission"] == sub]
        multi = mine[0]["multi_trial_groups"]
        total = mine[0]["groups_in_submission"]
        for stratum, weight in (
            ("multi-trial", multi),
            ("single-trial", total - multi),
        ):
            part = [r for r in mine if r["stratum"] == stratum]
            if not part:
                continue
            k = [r for r in part if r["verdict"] in keep]
            num += weight * sum(_real(r["label"]) for r in k) / len(part)
            den += weight * len(k) / len(part)
    out["population_precision"] = round(num / den, 3) if den else None
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--trajectories", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--mode", choices=("on", "off"), default="on")
    parser.add_argument("--max-tokens", type=int, default=16384)
    parser.add_argument("--workers", type=int, default=64)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    rows = yaml.safe_load((HERE / "labels" / "scan_groups.yaml").read_text())
    cfg = ScanConfig(
        base_url=args.base_url,
        model=args.model,
        workers=args.workers,
        max_tokens=args.max_tokens,
        thinking=args.mode == "on",
    )
    kb = load_kb()
    verdicts = []
    for sub in sorted({r["submission"] for r in rows}):
        mine = [r for r in rows if r["submission"] == sub]
        trials = {p.name: load_trial(p) for p in trial_dirs(args.trajectories / sub)}
        verdicts += list(
            zip(
                mine,
                adjudicate_groups(mine, trials, kb, cfg),
            )
        )
    out_rows = [{**row, **v} for row, v in verdicts]
    report = {"verdicts": Counter(r["verdict"] for r in out_rows)}
    for split, keep_rows in (
        ("dev", [r for r in out_rows if r["submission"] in DEV]),
        ("test", [r for r in out_rows if r["submission"] not in DEV]),
    ):
        report[split] = {
            "no filter": metrics(keep_rows, set(r["verdict"] for r in keep_rows)),
            "keep real": metrics(keep_rows, {"real"}),
            "keep real+unclear": metrics(keep_rows, {"real", "unclear"}),
            "confusion": Counter(f"{r['label']}->{r['verdict']}" for r in keep_rows),
        }
    report["errors"] = sum(bool(r["error"]) for r in out_rows)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"report": report, "rows": out_rows}, indent=1) + "\n"
    )
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
