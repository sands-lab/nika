"""Audit the submissions one after another with the scan pipeline.

Two stages, so the LLM work runs in parallel:

    # A: scan, group, and adjudicate every submission, all at once
    uv run python -m experiment.submission_audit.evolve prepare \\
        --trajectories /path/to/trajectories/0.2.0 \\
        --base-url http://host:port/v1 --model Qwen3.8-27B-FP8 \\
        --prepared runs/prepared

    # B: grow the memory over the submissions in one order
    uv run python -m experiment.submission_audit.evolve replay \\
        --trajectories /path/to/trajectories/0.2.0 --prepared runs/prepared \\
        --order shuffle --shuffle-seed 1 --out runs/pipeline-shuffle1 \\
        --base-url http://host:port/v1 --model Qwen3.8-27B-FP8

Neither the scan (packets carry the built-in checks' hits only) nor a group's
verdict depends on the memory, so stage A computes them once for every
submission. Stage B starts from the skill's built-in part and, submission by
submission, runs the deterministic audit with the grown memory, treats groups a
learned issue, or a built-in check that identifies the instance, already
fires on as recorded, takes the stored verdicts of the
others, and writes issue files for the real ones (``auditlib.pipeline``). The
cross-trial tables include earlier submissions, so stage B reads and judges
them with the LLM given by ``--base-url`` or ``--cli``.
Adjudication cost counts only the verdicts stage B uses. Each step keeps the
memory it started from (``NN_<submission>/kb_before``); ``map_issues.py`` and
``recall.py`` then score the run as they score the agent runs.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


from experiment.submission_audit import skill  # noqa: F401  (adds auditlib to sys.path)
from experiment.submission_audit.scan_eval import CLI

from auditlib.adjudicate import adjudicate_groups
from auditlib.aggregate import group_findings
from auditlib.kb import KB_DIR, load_kb
from auditlib.pipeline import review_submission, worth_judging
from auditlib.runner import audit_trajectories
from auditlib.scan import ScanConfig, scan_trials
from auditlib.trace import load_trial, trial_dirs


def builtin_kb_dir(dest: Path) -> Path:
    shutil.copytree(KB_DIR, dest, ignore=shutil.ignore_patterns("issues"))
    (dest / "issues").mkdir()
    return dest


def submissions(root: Path, order: str, shuffle_seed: int) -> list[Path]:
    paths = sorted(p for p in root.iterdir() if p.is_dir() and trial_dirs(p))
    if order == "shuffle":
        random.Random(shuffle_seed).shuffle(paths)
    return paths


def load_scan(path: Path) -> list[dict]:
    """Scan records, one per trial and sample; a retried record replaces the
    failed one before it."""
    records: dict[tuple, dict] = {}
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        records[rec["trial"], rec["sample"]] = rec
    return list(records.values())


def scan_one(pkg: Path, out: Path, scan: ScanConfig) -> bool:
    """Scan one submission, appending to ``scan.jsonl`` so a stop can resume.

    Returns whether every trial and sample scanned without a failed call."""
    out.mkdir(parents=True, exist_ok=True)
    path = out / "scan.jsonl"
    if (out / "scan.done").is_file():
        return True
    done = set()
    if path.is_file():
        done = {(r["trial"], r["sample"]) for r in load_scan(path) if not r["errors"]}
    with tempfile.TemporaryDirectory() as tmp:
        kb = load_kb(builtin_kb_dir(Path(tmp) / "kb"))
        hits = audit_trajectories(pkg, kb=kb).get("trials", {})
    with path.open("a", encoding="utf-8") as fh:

        def write(record: dict) -> None:
            fh.write(json.dumps(record) + "\n")
            fh.flush()

        records = scan_trials(
            trial_dirs(pkg), kb, scan, hits=hits, on_result=write, done=done
        )
    if any(r["errors"] for r in records):
        return False
    (out / "scan.done").touch()
    return True


def adjudicate_one(pkg: Path, out: Path, adjudicate: ScanConfig) -> str:
    """Adjudicate the groups worth judging; others are stored as skipped."""
    verdict_path = out / "verdicts.json"
    if verdict_path.is_file():
        return pkg.name
    records = load_scan(out / "scan.jsonl")
    samples = max((r["sample"] for r in records), default=0) + 1
    with tempfile.TemporaryDirectory() as tmp:
        kb = load_kb(builtin_kb_dir(Path(tmp) / "kb"))
    trials = {p.name: load_trial(p) for p in trial_dirs(pkg)}
    groups = group_findings(records, trials)
    todo = [i for i, g in enumerate(groups) if worth_judging(g, samples)]
    judged = adjudicate_groups([groups[i] for i in todo], trials, kb, adjudicate)
    failed = sum(bool(v["error"]) for v in judged)
    if failed:
        return f"{pkg.name}: {failed} adjudications failed, rerun to retry"
    verdicts: list[dict] = [
        {"verdict": "skipped", "reason": "", "tokens": [0, 0], "error": ""}
        for _ in groups
    ]
    for i, v in zip(todo, judged):
        verdicts[i] = v
    verdict_path.write_text(json.dumps(verdicts) + "\n")
    return pkg.name


def cmd_prepare(args: argparse.Namespace) -> None:
    if not (args.base_url and args.model) and not args.cli:
        raise SystemExit("prepare needs --base-url and --model, or --cli")
    pkgs = submissions(args.trajectories, "date", 0)
    command = CLI.get(args.cli)
    scan = ScanConfig(
        base_url=args.base_url,
        model=args.model,
        samples=args.samples,
        workers=args.workers_per_submission,
        thinking=False,
        command=command,
        timeout=600,
    )
    adjudicate = ScanConfig(
        base_url=args.base_url,
        model=args.model,
        workers=args.workers_per_submission,
        max_tokens=16384,
        thinking=True,
        command=command,
        timeout=600,
    )

    # Each submission is adjudicated as soon as its own scan is done, so
    # replicas stay busy while slow submissions finish scanning.
    def one(pkg: Path) -> str:
        if not scan_one(pkg, args.prepared / pkg.name, scan):
            return f"{pkg.name}: scan calls failed, rerun to retry"
        return adjudicate_one(pkg, args.prepared / pkg.name, adjudicate)

    with ThreadPoolExecutor(args.parallel_submissions) as pool:
        for name in pool.map(one, pkgs):
            print(f"prepared {name}", flush=True)


def cmd_replay(args: argparse.Namespace) -> None:
    out = args.out
    # Cross-trial statistics depend on the order, so their scan and verdicts
    # run here; without an endpoint the cohort stage is skipped.
    cohort_cfg = (
        ScanConfig(
            base_url=args.base_url,
            model=args.model,
            workers=args.workers,
            max_tokens=16384,
            thinking=True,
            command=CLI.get(args.cli),
            timeout=600,
        )
        if args.base_url or args.cli
        else None
    )
    kb_dir = out / "kb"
    if not kb_dir.is_dir():
        builtin_kb_dir(kb_dir)
    log_path = out / "replay.json"
    log = json.loads(log_path.read_text()) if log_path.is_file() else []
    history: list = []
    order = submissions(args.trajectories, args.order, args.shuffle_seed)
    for index, pkg in enumerate(order):
        step = out / f"{index:02d}_{pkg.name}"
        prepared = args.prepared / pkg.name
        records = load_scan(prepared / "scan.jsonl")
        verdicts = json.loads((prepared / "verdicts.json").read_text())
        if index < len(log):
            # Finished step: only rebuild the cohort history.
            audit_trajectories(
                pkg,
                kb=load_kb(step / "kb_before"),
                history=list(history),
                records_out=history,
            )
            continue
        if (step / "kb_before").is_dir():  # interrupted: restart from its memory
            shutil.rmtree(kb_dir)
            shutil.copytree(step / "kb_before", kb_dir)
        else:
            shutil.copytree(kb_dir, step / "kb_before")
        pre = {"records": records, "verdicts": verdicts}
        if args.reuse_cohort:
            pre["cohort"] = json.loads(
                (args.reuse_cohort / step.name / "work" / "cohort.json").read_text()
            )
        trial_records: list = []
        entry = review_submission(
            pkg,
            kb_dir,
            step / "work",
            scan=None,
            adjudicate=cohort_cfg,
            history=history,
            records_out=trial_records,
            precomputed=pre,
        )
        history.extend(trial_records)
        entry["index"] = index
        log.append(entry)
        log_path.write_text(json.dumps(log, indent=1) + "\n")
        print(
            f"[{index:02d}] {pkg.name}: groups {entry['groups']} recorded "
            f"{entry['recorded']} adjudicated {entry['adjudicated']} real "
            f"{entry['real']} written {len(entry['written'])} cohort "
            f"{entry['cohort']['findings']}/{entry['cohort']['real']}/"
            f"{len(entry['cohort']['written'])}",
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="Scan, group, and adjudicate every submission.")
    p.add_argument("--trajectories", type=Path, required=True)
    p.add_argument("--prepared", type=Path, required=True)
    p.add_argument("--base-url", default="")
    p.add_argument("--model", default="")
    p.add_argument("--cli", choices=tuple(CLI), help="Agent CLI instead of HTTP.")
    p.add_argument("--samples", type=int, default=3, help="Scans per trial.")
    p.add_argument("--parallel-submissions", type=int, default=21)
    p.add_argument("--workers-per-submission", type=int, default=8)
    p.set_defaults(func=cmd_prepare)
    p = sub.add_parser("replay", help="Replay the memory over the submissions.")
    p.add_argument("--trajectories", type=Path, required=True)
    p.add_argument("--prepared", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--order", default="date", choices=("date", "shuffle"))
    p.add_argument("--shuffle-seed", type=int, default=0)
    p.add_argument("--base-url", help="LLM endpoint for the cross-trial stage.")
    p.add_argument("--model")
    p.add_argument("--cli", choices=tuple(CLI), help="Agent CLI instead of HTTP.")
    p.add_argument(
        "--reuse-cohort",
        type=Path,
        help="Earlier run of the same order whose cross-trial findings and "
        "verdicts to reuse instead of calling the LLM.",
    )
    p.add_argument("--workers", type=int, default=32)
    p.set_defaults(func=cmd_replay)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
