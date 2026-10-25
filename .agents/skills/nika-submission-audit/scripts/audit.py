"""Tools of the nika-submission-audit skill.

Run from a NIKA checkout, so the script uses NIKA's scoring and trace code:

    uv run python .agents/skills/nika-submission-audit/scripts/audit.py <command> ...

Commands:
  review      Full audit of one submission: audit, scan, group, adjudicate, record.
  run         Deterministic audit of a trajectories package or result directory.
  packet      Reviewer packet for one trial: header, hits, numbered steps.
  scan        Ask every trial the issue-class questions through an LLM endpoint.
  cite-check  Keep findings whose evidence occurs verbatim in the cited step.
  kb-list     List knowledge-base issues.
  kb-validate Load the knowledge base and report errors.
  kb-check    Check a proposed issue file: it loads and fires on its source trials.
  fetch       Download a submission's trajectories from the Hugging Face dataset.

Every command takes --kb to use a knowledge base other than the skill's own.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from nika.config import REPO_ROOT  # noqa: E402

# auditlib sits next to this script; experiment.audit (leak tokens) in the repo.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(REPO_ROOT))

from auditlib.kb import KB_DIR, KnowledgeBaseError, load_kb  # noqa: E402
from auditlib.trace import load_trial, trial_dirs  # noqa: E402

FAIL_ON = ("block", "warn", "never")


def cmd_run(args: argparse.Namespace) -> int:
    from auditlib.cohort import history_from_scores
    from auditlib.runner import (
        audit_trajectories,
        read_package,
        render_markdown,
        write_report,
    )

    if not trial_dirs(args.path):
        sys.exit(f"No trial directories under {args.path}")
    kb = load_kb(args.kb)
    past = []
    if args.history:
        release = read_package(args.path).package.get("release") or ""
        past = history_from_scores(
            args.history, release=str(release), exclude=args.path.name
        )
    report = audit_trajectories(args.path, kb=kb, history=past, repo=args.repo)
    if args.out:
        write_report(report, args.out)
    text = render_markdown(report)
    if args.markdown:
        args.markdown.write_text(text + "\n", encoding="utf-8")
    print(text)
    verdict = report["verdict"]
    if args.fail_on == "warn" and verdict in ("warn", "block"):
        return 1
    return 1 if args.fail_on == "block" and verdict == "block" else 0


def cmd_review(args: argparse.Namespace) -> int:
    from auditlib.cohort import history_from_scores
    from auditlib.pipeline import review_submission
    from auditlib.runner import read_package
    from auditlib.scan import ScanConfig

    if not args.base_url or not args.model:
        sys.exit("review needs --base-url and --model (or NIKA_AUDIT_LLM_URL/_MODEL)")
    if not trial_dirs(args.path):
        sys.exit(f"No trial directories under {args.path}")
    past = []
    if args.history:
        release = read_package(args.path).package.get("release") or ""
        past = history_from_scores(
            args.history, release=str(release), exclude=args.path.name
        )
    key = os.environ.get("NIKA_AUDIT_LLM_KEY", "none")
    scan = ScanConfig(
        base_url=args.base_url,
        model=args.model,
        api_key=key,
        samples=args.samples,
        workers=args.workers,
        thinking=False,
    )
    judge = ScanConfig(
        base_url=args.base_url,
        model=args.model,
        api_key=key,
        workers=args.workers,
        max_tokens=16384,
        thinking=True,
    )
    kb_dir = args.kb or KB_DIR
    summary = review_submission(
        args.path,
        kb_dir,
        args.workdir,
        scan=scan,
        adjudicate=judge,
        history=past,
        repo=args.repo,
    )
    print(json.dumps(summary, indent=2))
    return 0


def cmd_packet(args: argparse.Namespace) -> int:
    from auditlib.review import review_packet

    trial = load_trial(args.trial)
    hits = []
    if args.audit:
        report = json.loads(args.audit.read_text(encoding="utf-8"))
        hits = report.get("trials", {}).get(trial.trial_id, [])
    print(review_packet(trial, hits, full_steps=args.full_step))
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    from collections import Counter

    from auditlib.scan import ScanConfig, scan_trials

    if not args.base_url or not args.model:
        sys.exit("scan needs --base-url and --model (or NIKA_AUDIT_LLM_URL/_MODEL)")
    paths = trial_dirs(args.path)
    if args.trials:
        wanted = set(args.trials)
        paths = [p for p in paths if p.name in wanted]
    hits = {}
    if args.audit:
        hits = json.loads(args.audit.read_text(encoding="utf-8")).get("trials", {})
    cfg = ScanConfig(
        base_url=args.base_url,
        model=args.model,
        api_key=os.environ.get("NIKA_AUDIT_LLM_KEY", "none"),
        samples=args.samples,
        workers=args.workers,
        max_tokens=args.max_tokens,
        thinking={"on": True, "off": False}.get(args.thinking),
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:

        def write(record: dict) -> None:
            fh.write(json.dumps(record) + "\n")
            fh.flush()

        records = scan_trials(paths, load_kb(args.kb), cfg, hits=hits, on_result=write)
    supported = Counter(
        f["class"]
        for r in records
        for f in r["findings"]
        if f["citation_status"] == "supported"
    )
    errors = sum(bool(r["errors"]) for r in records)
    print(f"{len(records)} scans, {errors} with errors; supported findings by class:")
    for cls, n in supported.most_common():
        print(f"  {cls:24} {n}")
    return 0


def cmd_cite_check(args: argparse.Namespace) -> int:
    from auditlib.review import check_citations

    items = json.loads(args.findings.read_text(encoding="utf-8"))
    if isinstance(items, dict):
        items = items.get("findings", [])
    wanted = {str(f.get("trial")) for f in items}
    trials = {
        p.name: load_trial(p) for p in trial_dirs(args.trajectories) if p.name in wanted
    }
    checked = check_citations(items, trials)
    text = json.dumps(checked, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    ok = sum(1 for f in checked if f["citation_status"] == "supported")
    print(f"{ok}/{len(checked)} findings have verified evidence.", file=sys.stderr)
    return 0


def cmd_kb_list(args: argparse.Namespace) -> int:
    kb = load_kb(args.kb)
    print(f"knowledge base: {len(kb.issues)} issues")
    for issue in sorted(kb.issues, key=lambda i: (i.issue_class, i.id)):
        if args.role and issue.role != args.role:
            continue
        print(
            f"{issue.id:44} {issue.issue_class:20} {issue.role:5} {issue.severity:5} "
            f"{issue.status:9} {issue.title}"
        )
    return 0


def cmd_kb_validate(args: argparse.Namespace) -> int:
    try:
        kb = load_kb(args.kb)
    except KnowledgeBaseError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"knowledge base: {len(kb.issues)} issues, {len(kb.taxonomy)} classes")
    return 0


def cmd_kb_check(args: argparse.Namespace) -> int:
    from auditlib.review import check_issue, load_issue_file

    try:
        issue = load_issue_file(args.issue)
    except KnowledgeBaseError as exc:
        print(exc, file=sys.stderr)
        return 1
    report = check_issue(issue, sources=args.source, kb=load_kb(args.kb))
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


def cmd_fetch(args: argparse.Namespace) -> int:
    from auditlib.fetch import fetch_trajectories

    print(
        fetch_trajectories(args.slug, args.release, args.dest, revision=args.revision)
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--kb", type=Path, help="Knowledge base directory.")
        p.set_defaults(func=func)
        return p

    p = add("run", cmd_run, "Deterministic audit.")
    p.add_argument(
        "path", type=Path, help="Trajectories package, result directory, or trial."
    )
    p.add_argument("--out", type=Path, help="Write audit.json here.")
    p.add_argument("--markdown", type=Path, help="Write the Markdown summary here.")
    p.add_argument(
        "--history",
        type=Path,
        help="nika-leaderboard checkout with earlier submissions.",
    )
    p.add_argument(
        "--repo",
        type=Path,
        help="NIKA git checkout, to detect regressions after fixes.",
    )
    p.add_argument("--fail-on", choices=FAIL_ON, default="never")

    p = add("packet", cmd_packet, "Reviewer packet for one trial.")
    p.add_argument("trial", type=Path)
    p.add_argument("--audit", type=Path, help="audit.json whose hits are attached.")
    p.add_argument("--full-step", type=int, action="append", default=[])

    p = add("review", cmd_review, "Full audit of one submission.")
    p.add_argument("path", type=Path, help="Trajectories package or result directory.")
    p.add_argument(
        "--workdir",
        type=Path,
        required=True,
        help="Directory for intermediate results.",
    )
    p.add_argument("--history", type=Path, help="nika-leaderboard checkout.")
    p.add_argument(
        "--repo",
        type=Path,
        help="NIKA git checkout, to detect regressions after fixes.",
    )
    p.add_argument("--base-url", default=os.environ.get("NIKA_AUDIT_LLM_URL"))
    p.add_argument("--model", default=os.environ.get("NIKA_AUDIT_LLM_MODEL"))
    p.add_argument("--samples", type=int, default=3, help="Scans per trial.")
    p.add_argument("--workers", type=int, default=64, help="Parallel requests.")

    p = add("scan", cmd_scan, "Scan every trial with the issue-class questions.")
    p.add_argument("path", type=Path, help="Trajectories package or result directory.")
    p.add_argument("--out", type=Path, required=True, help="JSON lines, one per scan.")
    p.add_argument("--audit", type=Path, help="audit.json whose hits are attached.")
    p.add_argument("--base-url", default=os.environ.get("NIKA_AUDIT_LLM_URL"))
    p.add_argument("--model", default=os.environ.get("NIKA_AUDIT_LLM_MODEL"))
    p.add_argument("--samples", type=int, default=1, help="Scans per trial.")
    p.add_argument("--workers", type=int, default=64, help="Parallel requests.")
    p.add_argument("--trials", nargs="*", help="Only these trial ids.")
    p.add_argument("--max-tokens", type=int, default=8192)
    p.add_argument(
        "--thinking",
        choices=("on", "off", "default"),
        default="default",
        help="Reasoning mode, for endpoints that support enable_thinking.",
    )

    p = add("cite-check", cmd_cite_check, "Verify findings' citations.")
    p.add_argument("findings", type=Path)
    p.add_argument("--trajectories", type=Path, required=True)
    p.add_argument("--out", type=Path)

    p = add("kb-list", cmd_kb_list, "List knowledge-base issues.")
    p.add_argument("--role", choices=("known", "lead"))

    add("kb-validate", cmd_kb_validate, "Validate the knowledge base.")

    p = add("kb-check", cmd_kb_check, "Check a proposed issue file.")
    p.add_argument("issue", type=Path)
    p.add_argument(
        "--source", type=Path, action="append", default=[], help="Source trial."
    )

    p = add("fetch", cmd_fetch, "Download trajectories from the Hub.")
    p.add_argument("slug")
    p.add_argument("--release", required=True)
    p.add_argument("--dest", type=Path, default=Path("audit-trajectories"))
    p.add_argument("--revision")

    args = parser.parse_args()
    jail = os.environ.get("NIKA_AUDIT_KB_JAIL")
    if jail and args.kb is not None:
        # Experiments confine an auditing agent to its own knowledge base.
        if not args.kb.resolve().is_relative_to(Path(jail).resolve()):
            sys.exit(f"--kb must be inside {jail}")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
