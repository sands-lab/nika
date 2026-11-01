"""Per-trial scanning against the reviewer agents, both starting from no memory.

    uv run python -m experiment.submission_audit.scan_eval \\
        --trajectories /path/to/trajectories/0.2.0 --runs /path/to/runs \\
        --base-url http://host:port/v1 --model Qwen3.8-27B-FP8 \\
        --mode off --samples 3 --out runs/scan-off3

The test submissions are the first submission of each run in ``--runs``:
there the run's reviewer agents started with an empty issue memory, as the
scanner does. The scanner asks every trial the issue-class questions of the
skill's taxonomy (``audit.py scan``), with the built-in checks' hits attached.

A reference issue counts as found by the scanner in a submission when a
finding with a verified citation sits in a trial that shows the issue
(``trial``), or cites a step that shows it (``step``). Trials and steps that
show a reference issue are those where a learned issue recording it fires
(``issue_refs.yaml``). The run's reviewers found it when the issues the run
wrote in that first step map to it (``mapping_llm.yaml``). Both are counted over
the reference issues that have trial labels in that submission.
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import time
import tomllib
from collections import defaultdict
from pathlib import Path

import yaml

from experiment.submission_audit import skill  # noqa: F401  (adds auditlib to sys.path)

from auditlib.detectors import run_trial
from auditlib.kb import KB_DIR, load_kb
from auditlib.runner import audit_trajectories
from auditlib.scan import ScanConfig, scan_trials
from auditlib.trace import load_trial, trial_dirs

HERE = Path(__file__).resolve().parent


def _codex_without_mcp() -> tuple[str, ...]:
    """Flags that disable the user's Codex MCP servers, which would otherwise
    start on every call."""
    config = Path.home() / ".codex" / "config.toml"
    servers = (
        tomllib.loads(config.read_text()).get("mcp_servers", {})
        if config.is_file()
        else {}
    )
    return tuple(
        x for name in servers for x in ("-c", f"mcp_servers.{name}.enabled=false")
    )


# Agent CLIs that can stand in for the HTTP endpoint (prompt on stdin).
_CODEX = ("codex", "exec", "--skip-git-repo-check", "-s", "read-only")
_NO_MCP = _codex_without_mcp()
CLI = {
    "codex": (*_CODEX, *_NO_MCP, "-"),
    "luna": (
        *_CODEX,
        *_NO_MCP,
        "-m",
        "gpt-5.6-luna",
        "-c",
        "model_reasoning_effort=low",
        "-",
    ),
    # Used for the Composer row of the README.
    "cursor": (
        "agent",
        "-p",
        "--trust",
        "--output-format",
        "text",
        "--model",
        "composer-2.5",
    ),
}


def first_steps(runs: Path) -> dict[str, Path]:
    """Run name -> its first step directory."""
    out = {}
    for run in sorted(runs.iterdir()):
        steps = sorted(p for p in run.glob("00_*") if p.is_dir())
        if steps and (run / "mapping_llm.yaml").is_file():
            out[run.name] = steps[0]
    return out


def labels(pkg: Path, issue_refs: dict[str, list[str]]) -> dict[str, set]:
    """Reference issue -> {(trial, step)} where a recording issue fires."""
    kb = load_kb()
    out: dict[str, set] = defaultdict(set)
    for path in trial_dirs(pkg):
        trial = load_trial(path)
        for hit in run_trial(kb, trial):
            if hit.step is None:
                continue
            for ref in issue_refs.get(hit.issue, ()):
                out[ref].add((trial.trial_id, hit.step))
    return out


def run_written(step: Path, run: Path) -> set[str]:
    """Reference issues the run's first step wrote issues for."""
    mapping = yaml.safe_load((run / "mapping_llm.yaml").read_text(encoding="utf-8"))
    before = {p.stem for p in (step / "kb_before" / "issues").glob("*.yaml")}
    nxt = sorted(p for p in run.glob("01_*") if p.is_dir())
    after_dir = nxt[0] / "kb_before" if nxt else run / "kb"
    after = {p.stem for p in (after_dir / "issues").glob("*.yaml")}
    return {r for i in after - before for r in mapping.get(i) or []}


def builtin_hits(pkg: Path) -> dict[str, list]:
    with tempfile.TemporaryDirectory() as tmp:
        kb_dir = Path(tmp) / "kb"
        shutil.copytree(KB_DIR, kb_dir, ignore=shutil.ignore_patterns("issues"))
        (kb_dir / "issues").mkdir()
        report = audit_trajectories(pkg, kb=load_kb(kb_dir))
    return report.get("trials", {})


def score(
    records: list[dict], lab: dict[str, set], ref_class: dict[str, str]
) -> dict[str, set[str]]:
    cited_steps, cited_trials = set(), defaultdict(set)
    for rec in records:
        for f in rec["findings"]:
            if f["citation_status"] != "supported":
                continue
            cited_steps.add((rec["trial"], f["step"]))
            cited_trials[rec["trial"]].add(f["class"])
    step, trial = set(), set()
    for ref, where in lab.items():
        if where & cited_steps:
            step.add(ref)
        if any(ref_class[ref] in cited_trials.get(t, ()) for t, _ in where):
            trial.add(ref)
    return {"step": step, "trial": trial}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--trajectories", type=Path, required=True)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--cli", choices=tuple(CLI), help="Agent CLI instead of HTTP.")
    parser.add_argument("--mode", choices=("on", "off"), default="off")
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--workers", type=int, default=128)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    reference = yaml.safe_load((HERE / "reference_issues.yaml").read_text())
    ref_class = {r["id"]: r["class"] for r in reference}
    issue_refs = yaml.safe_load((HERE / "issue_refs.yaml").read_text())
    cfg = ScanConfig(
        base_url=args.base_url,
        model=args.model,
        samples=args.samples,
        workers=args.workers,
        max_tokens=args.max_tokens,
        thinking=args.mode == "on",
        command=CLI.get(args.cli),
        timeout=600,
    )
    kb = load_kb()
    summary = []
    for run, step in first_steps(args.runs).items():
        pkg = args.trajectories / step.name.split("_", 1)[1]
        out = args.out / f"{pkg.name}.jsonl"
        lab = labels(pkg, issue_refs)
        if out.is_file():
            records = [json.loads(line) for line in out.read_text().splitlines()]
            wall = None
        else:
            t0 = time.time()
            records = scan_trials(trial_dirs(pkg), kb, cfg, hits=builtin_hits(pkg))
            wall = round(time.time() - t0)
            out.write_text("".join(json.dumps(r) + "\n" for r in records))
        found = score(records, lab, ref_class)
        by_sample = [
            score([r for r in records if r["sample"] == s], lab, ref_class)
            for s in sorted({r["sample"] for r in records})
        ]
        written = run_written(step, args.runs / run) & set(lab)
        findings = [f for r in records for f in r["findings"]]
        row = {
            "run": run,
            "submission": pkg.name,
            "labelled_refs": len(lab),
            "run_reviewers": len(written),
            "scan_trial": len(found["trial"]),
            "scan_step": len(found["step"]),
            "scan_trial_per_sample": [len(s["trial"]) for s in by_sample],
            "trials": len({r["trial"] for r in records}),
            "findings": len(findings),
            "supported": sum(f["citation_status"] == "supported" for f in findings),
            "errors": sum(bool(r["errors"]) for r in records),
            "truncated": sum(r["truncated"] for r in records),
            "prompt_tokens": sum(r["prompt_tokens"] for r in records),
            "completion_tokens": sum(r["completion_tokens"] for r in records),
            "wall_sec": wall,
            "found_trial": sorted(found["trial"]),
            "found_by_run": sorted(written),
        }
        summary.append(row)
        print(
            f"{run:9s} {pkg.name[:40]:40s} refs {row['labelled_refs']:3d} "
            f"run {row['run_reviewers']:2d} scan(trial) {row['scan_trial']:2d} "
            f"scan(step) {row['scan_step']:2d} per-sample {row['scan_trial_per_sample']} "
            f"wall {wall}s tokens {row['prompt_tokens']}/{row['completion_tokens']}",
            flush=True,
        )
    (args.out / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")


if __name__ == "__main__":
    main()
