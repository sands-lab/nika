"""Tools of the nika-submission-audit skill.

Submission validation runs deterministic checks; CI runs model review.
The skill and review commands also remain available through:

    uv run python -m nika.audit.cli <command> ...

Commands:
  review      Full audit of one submission: audit, scan, group, adjudicate, record.
  run         Deterministic audit of a trajectories package or result directory.
  packet      Reviewer packet for one trial: header, hits, numbered steps.
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

from nika.audit.kb import KnowledgeBaseError, load_kb
from nika.audit.trace import load_trial, trial_dirs


def cmd_run(args: argparse.Namespace) -> int:
    from nika.audit.cohort import history_from_scores
    from nika.audit.runner import (
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
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    from nika.workflows.leaderboard.review import review_trajectories

    summary = review_trajectories(
        args.path,
        args.workdir,
        base_url=args.base_url,
        model=args.model,
        kb=args.kb,
        repo=args.repo,
        history=args.history,
        samples=args.samples,
        workers=args.workers,
    )
    print(json.dumps(summary, indent=2))
    return 0 if summary["complete"] else 1


def cmd_packet(args: argparse.Namespace) -> int:
    from nika.audit.review import review_packet

    trial = load_trial(args.trial)
    hits = []
    if args.audit:
        report = json.loads(args.audit.read_text(encoding="utf-8"))
        hits = report.get("trials", {}).get(trial.trial_id, [])
    print(review_packet(trial, hits, full_steps=args.full_step))
    return 0


def cmd_cite_check(args: argparse.Namespace) -> int:
    from nika.audit.review import check_citations

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
    from nika.audit.review import check_issue, load_issue_file

    try:
        issue = load_issue_file(args.issue)
    except KnowledgeBaseError as exc:
        print(exc, file=sys.stderr)
        return 1
    report = check_issue(issue, sources=args.source, kb=load_kb(args.kb))
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


def cmd_fetch(args: argparse.Namespace) -> int:
    from nika.audit.fetch import fetch_trajectories

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
    p.add_argument("--workers", type=int, default=2, help="Parallel review calls.")

    p = add("cite-check", cmd_cite_check, "Verify findings' citations.")
    p.add_argument("findings", type=Path)
    p.add_argument("--trajectories", type=Path, required=True)
    p.add_argument("--out", type=Path)

    add("kb-list", cmd_kb_list, "List knowledge-base issues.")

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
