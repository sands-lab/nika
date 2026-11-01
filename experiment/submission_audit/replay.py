"""Replay leaderboard submissions through the auditing agent, one at a time.

For each submission, the deterministic audit (``scripts/audit.py run``) runs
with the agent's current issue memory, parallel reviewer agents read the
trajectories, and a curator agent writes confirmed new issues into the
memory. Issue files that fail the basic checks (load, valid fields, fire on
their own source trials) are moved aside. The next submission starts from the
grown memory. Recall against
``reference_issues.yaml`` is computed afterwards by ``recall.py``.

    uv run python -m experiment.submission_audit.replay \
        --trajectories /path/to/nika-trajectories/trajectories/0.2.0 \
        --agent-cmd 'claude -p --model claude-opus-5-5 --permission-mode acceptEdits' \
        --order shuffle --shuffle-seed 1 --out runs/shuffle1

Every run starts from the skill's built-in part only: ``SKILL.md``, the method
references, and the generic checks in ``kb/checks``. The learned part (the
skill's ``kb/issues`` and ``references/lessons.md``) is left out; each run
grows its own.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shlex
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from experiment.submission_audit.skill import (
    REPO_ROOT,
    SKILL_DIR,
)  # adds auditlib to sys.path
from auditlib.cohort import TrialRecord
from auditlib.kb import KB_DIR, KnowledgeBaseError, load_kb
from auditlib.review import check_issue, load_issue_file
from auditlib.runner import audit_trajectories, render_markdown, write_report
from auditlib.trace import trial_dirs

REVIEWER_PROMPT = """\
You are reviewer {i} of {n} auditing the trajectories at {traj}, following
SKILL.md in your working directory. The deterministic audit already ran with
the knowledge base in ./kb (read it, do not change it): read {audit_md}
first. Do steps 2 and 3 only, for your assignment below; the other reviewers
cover the rest.

Assignment (lead, why it fired, trials to review):
{assignment}

Tools: Bash runs only `./audit ...`; every other shell command (ls, cat,
mkdir, python) is refused and wastes a turn. Read files with Read, list them
with Glob, and write them with Write or Edit. Your directories already exist.

Budget: at most {budget} packets and about {turns} turns. Open trials only
with `./audit packet <trial dir> --audit {audit}`; do not read raw
messages.jsonl files. Save progress as you go, because you may be stopped:
after each packet, append any finding to {work}/findings.json (a JSON list)
right away; when a finding survives the skeptic and no KB issue explains it,
write its candidate issue file (curator guide format, source trials as
`<trial_id>:<step>` in provenance.refs) to {work}/candidates/ immediately.
Run `./audit cite-check {work}/findings.json --trajectories {traj} --out
{work}/findings.checked.json` before you finish. When your packets are done,
write {work}/notes.txt (one line per finding: trial, class, title, verdict,
candidate file) and stop.
"""

CURATOR_PROMPT = """\
You are the curator for the audit of the trajectories at {traj}, following
SKILL.md in your working directory (step 4). {n} reviewers worked in
parallel; their results are in {reviewers}: each has notes.txt,
findings.checked.json, and candidates/*.yaml. Read the deterministic audit
summary {audit_md} and the knowledge base in ./kb.

Tools: Bash runs only `./audit ...`; every other shell command (ls, cat,
mkdir, python) is refused and wastes a turn. Read files with Read, list them
with Glob, and write them with Write or Edit. Your directories already exist.

Merge the candidates: drop duplicates of each other and of existing KB
issues (extend an existing issue's signature instead when it should have
caught the finding), and write each remaining issue to ./kb/issues/ as one
file, keeping the source trials in provenance.refs. Check each with
`./audit kb-check <file> --source <trial dir>` for each of its source trials.
Finish by writing {notes} with one line per issue: id, class, sources, check
result.
"""

# The learned part of the skill, left out of every run.
_LEARNED = ("lessons.md",)
_FILE_TOOLS = ("Read", "Glob", "Grep", "Edit", "Write")


def build_workspace(
    out: Path, kb_dir: Path, *, trajectories: Path
) -> tuple[Path, Path, Path]:
    """An isolated working directory for the auditing agents, and their settings.

    The agent sees the skill's built-in part (``SKILL.md`` and the method
    references), copies of the scripts, its own knowledge base, and the
    trajectories. Claude Code deny
    rules hide the repository (the skill's learned part, the reference issues),
    the home directory, and every other run.
    """
    ws = out / "workspace"
    shutil.rmtree(ws, ignore_errors=True)
    ws.mkdir(parents=True)
    shutil.copy(SKILL_DIR / "SKILL.md", ws / "SKILL.md")
    shutil.copytree(
        SKILL_DIR / "references",
        ws / "references",
        ignore=shutil.ignore_patterns(*_LEARNED),
    )
    shutil.copytree(
        SKILL_DIR / "scripts",
        ws / "scripts",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (ws / "kb").symlink_to(kb_dir.resolve())
    wrapper = ws / "audit"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'exec uv run --quiet --project "$NIKA_AUDIT_PROJECT" python "{ws.resolve()}/scripts/audit.py" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    hidden = {
        REPO_ROOT,
        Path.home(),
        Path("/workspace/nika"),
        Path("/workspace/nika-leaderboard"),
        Path("/workspace/nika-state"),
        Path("/workspace/nika-audit-runs"),
    }
    hidden |= {
        p.resolve()
        for p in out.resolve().parent.iterdir()
        if p.resolve() != out.resolve()
    }
    # A rule on a directory that holds the workspace would hide it too.
    hidden -= set(out.resolve().parents)
    deny = [f"{tool}(/{p}/**)" for p in sorted(hidden) for tool in _FILE_TOOLS]
    deny += [f"{tool}(/{trajectories.resolve()}/**)" for tool in ("Edit", "Write")]
    settings = out / "agent_settings.json"
    settings.write_text(
        json.dumps({"permissions": {"deny": deny}}, indent=1), encoding="utf-8"
    )
    # Reviewers may read the knowledge base but only the curator writes it.
    kb_paths = {kb_dir.resolve(), (ws / "kb").absolute()}
    reviewer_deny = deny + [
        f"{tool}(/{p}/**)" for p in sorted(kb_paths) for tool in ("Edit", "Write")
    ]
    reviewer_settings = out / "reviewer_settings.json"
    reviewer_settings.write_text(
        json.dumps({"permissions": {"deny": reviewer_deny}}, indent=1),
        encoding="utf-8",
    )
    return ws, settings, reviewer_settings


def review_shards(
    report: dict,
    pkg: Path,
    n: int,
    *,
    sample_seed: int,
    per_lead: int = 4,
    sample: int = 2,
) -> list[list[dict]]:
    """Split the leads, plus random sample trials per environment, across
    *n* reviewers (round-robin, largest groups first)."""
    groups = []
    for lead in report.get("leads", []):
        trials, scenarios = [], set()
        for tid in lead.get("trials", []):
            env = tid.split("__", 1)[0]
            if env not in scenarios or len(trials) < 2:
                trials.append(tid)
                scenarios.add(env)
            if len(trials) >= per_lead:
                break
        metric = "; ".join(
            f"{m['metric']}={m['value']} @ {m['group']}"
            for m in lead.get("metrics", [])[:2]
        )
        groups.append(
            {
                "lead": lead["id"],
                "why": lead["title"] + (f" ({metric})" if metric else ""),
                "trials": trials,
            }
        )
    # Generic leads fire on most trials, so sample per environment from the
    # trials no lead group already assigns: issues with no lead get a look.
    assigned = {t for g in groups for t in g["trials"]}
    by_env: dict[str, list[str]] = {}
    for path in trial_dirs(pkg):
        if path.name not in assigned:
            by_env.setdefault(path.name.split("__", 1)[0], []).append(path.name)
    rng = random.Random(sample_seed)
    for env, names in sorted(by_env.items()):
        groups.append(
            {
                "lead": f"SAMPLE:{env}",
                "why": "random trials not assigned to a lead",
                "trials": rng.sample(names, min(sample, len(names))),
            }
        )
    groups.sort(key=lambda g: -len(g["trials"]))
    shards: list[list[dict]] = [[] for _ in range(n)]
    for k, group in enumerate(groups):
        shards[k % n].append(group)
    return [s for s in shards if s]


def _format_assignment(shard: list[dict], pkg: Path) -> str:
    lines = []
    for g in shard:
        lines.append(f"- {g['lead']}: {g['why']}")
        lines.extend(f"    {pkg}/trials/{t}" for t in g["trials"] if t)
    return "\n".join(lines)


def _text(value: bytes | str | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value or ""


def _run_agent(
    cmd: list[str], *, cwd: Path, env: dict, prompt: str, timeout: int, log: Path
) -> dict:
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        stdout, stderr, code = _text(exc.stdout), _text(exc.stderr), "timeout"
    log.with_suffix(".stdout.txt").write_text(stdout, encoding="utf-8")
    log.with_suffix(".stderr.txt").write_text(stderr, encoding="utf-8")
    return {"sec": round(time.time() - t0, 1), "exit": code, **_agent_usage(stdout)}


def write_markdown(report: dict, path: Path) -> None:
    path.write_text(render_markdown(report) + "\n", encoding="utf-8")


def _agent_usage(stdout: str) -> dict:
    """Cost and turns from ``claude -p --output-format json``; empty otherwise."""
    try:
        data = json.loads(stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {}
    if not isinstance(data, dict):
        return {}
    usage = data.get("usage") or {}
    return {
        "agent_cost_usd": data.get("total_cost_usd"),
        "agent_turns": data.get("num_turns"),
        "agent_output_tokens": usage.get("output_tokens"),
    }


def submissions(root: Path, order: str, shuffle_seed: int) -> list[Path]:
    """Submission packages under *root*, by date prefix or shuffled."""
    paths = sorted(p for p in Path(root).iterdir() if p.is_dir() and trial_dirs(p))
    if order == "shuffle":
        random.Random(shuffle_seed).shuffle(paths)
    return paths


def _check_new_issues(kb_dir: Path, before: dict[str, bytes], traj: Path) -> list[dict]:
    """Run the basic checks on every issue file the agent added or changed.

    Files that do not load or do not fire on their own source trials are moved
    to ``invalid/`` next to the knowledge base.
    """
    results = []
    trials = {p.name: p for p in trial_dirs(traj)}
    for path in sorted((kb_dir / "issues").glob("*.yaml")):
        if before.get(path.name) == path.read_bytes():
            continue
        try:
            issue = load_issue_file(path)
            sources = [
                trials[ref.split(":")[0]]
                for ref in issue.provenance.refs
                if ref.split(":")[0] in trials
            ]
            report = check_issue(issue, sources=sources, kb=load_kb(kb_dir))
        except (KnowledgeBaseError, ValueError, TypeError, yaml.YAMLError) as exc:
            report = {"issue": path.stem, "ok": False, "error": str(exc)}
        if not report.get("ok"):
            invalid = kb_dir.parent / "invalid"
            invalid.mkdir(exist_ok=True)
            shutil.move(str(path), invalid / path.name)
        results.append(report)
    return results


def replay(args: argparse.Namespace) -> None:
    out = Path(args.out)
    if REPO_ROOT in out.resolve().parents:
        raise SystemExit(
            "--out must be outside the NIKA checkout the agents cannot read"
        )
    if not args.resume and any(out.glob("[0-9][0-9]_*")):
        raise SystemExit(f"{out} holds an earlier run; pass --resume or a new --out")
    out.mkdir(parents=True, exist_ok=True)
    log: list[dict] = []
    if args.resume and (out / "replay.json").is_file():
        kb_dir = out / "kb"
        log = json.loads((out / "replay.json").read_text(encoding="utf-8"))
    elif args.start_kb:
        kb_dir = out / "kb"
        shutil.rmtree(kb_dir, ignore_errors=True)
        shutil.copytree(args.start_kb, kb_dir)
    else:
        # Built-in part only: taxonomy, policy, generic checks; no issues.
        kb_dir = out / "kb"
        shutil.rmtree(kb_dir, ignore_errors=True)
        shutil.copytree(KB_DIR, kb_dir, ignore=shutil.ignore_patterns("issues"))
        (kb_dir / "issues").mkdir()
    history: list[TrialRecord] = []
    workspace, settings, reviewer_settings = build_workspace(
        out, kb_dir, trajectories=Path(args.trajectories)
    )
    packages = submissions(Path(args.trajectories), args.order, args.shuffle_seed)
    if args.only:
        packages = [p for p in packages if p.name in set(args.only)]
    for index, pkg in enumerate(packages):
        if args.limit and index >= args.limit:
            break
        step = out / f"{index:02d}_{pkg.name}"
        if index < len(log):
            # Resumed: rebuild history from the finished step's own snapshot.
            audit_trajectories(
                pkg,
                kb=load_kb(step / "kb_before"),
                history=list(history),
                records_out=history,
            )
            continue
        if step.is_dir() and (step / "kb_before").is_dir():
            # Interrupted step: start it again from its knowledge base.
            shutil.rmtree(kb_dir)
            shutil.copytree(step / "kb_before", kb_dir)
            shutil.rmtree(step)
        step.mkdir(exist_ok=True)
        kb = load_kb(kb_dir)
        shutil.copytree(kb_dir, step / "kb_before", dirs_exist_ok=True)
        started = time.time()
        records: list[TrialRecord] = []
        report = audit_trajectories(
            pkg,
            kb=kb,
            history=list(history),
            repo=Path(args.repo) if args.repo else None,
            records_out=records,
        )
        write_report(report, step / "audit.json")
        entry = {
            "index": index,
            "submission": pkg.name,
            "kb_issues": len(kb.issues),
            "deterministic_sec": round(time.time() - started, 1),
            "known_issues": [r["id"] for r in report["known_issues"]],
            "leads": [r["id"] for r in report["leads"]],
        }
        before = {p.name: p.read_bytes() for p in (kb_dir / "issues").glob("*.yaml")}
        write_markdown(report, step / "audit.md")
        work = (step / "work").resolve()
        work.mkdir(exist_ok=True)
        env = {
            **os.environ,
            "NIKA_AUDIT_PROJECT": str(REPO_ROOT),
            "NIKA_AUDIT_KB_JAIL": str(kb_dir.resolve()),
        }
        base = shlex.split(args.agent_cmd)
        shards = review_shards(report, pkg, args.reviewers, sample_seed=index)
        entry["shards"] = [[g["lead"] for g in sh] for sh in shards]
        t0 = time.time()
        jobs = []
        with ThreadPoolExecutor(max_workers=len(shards)) as pool:
            for i, shard in enumerate(shards, start=1):
                rdir = work / f"reviewer_{i}"
                (rdir / "candidates").mkdir(parents=True, exist_ok=True)
                prompt = REVIEWER_PROMPT.format(
                    i=i,
                    n=len(shards),
                    traj=pkg,
                    audit_md=(step / "audit.md").resolve(),
                    budget=args.packet_budget,
                    turns=args.reviewer_turns,
                    audit=(step / "audit.json").resolve(),
                    assignment=_format_assignment(shard, pkg),
                    work=rdir,
                )
                jobs.append(
                    pool.submit(
                        _run_agent,
                        [*base, "--settings", str(reviewer_settings)],
                        cwd=workspace,
                        env=env,
                        prompt=prompt,
                        timeout=args.agent_timeout,
                        log=step / f"reviewer_{i}",
                    )
                )
        entry["reviewers"] = [j.result() for j in jobs]
        prompt = CURATOR_PROMPT.format(
            traj=pkg,
            n=len(shards),
            reviewers=", ".join(
                str(work / f"reviewer_{i}") for i in range(1, len(shards) + 1)
            ),
            audit_md=(step / "audit.md").resolve(),
            notes=(step / "agent_notes.txt").resolve(),
        )
        candidates = [
            p
            for i in range(1, len(shards) + 1)
            for p in (work / f"reviewer_{i}" / "candidates").glob("*.yaml")
        ]
        entry["candidates"] = len(candidates)
        if candidates:
            entry["curator"] = _run_agent(
                [
                    *base,
                    "--max-turns",
                    str(args.curator_turns),
                    "--settings",
                    str(settings),
                ],
                cwd=workspace,
                env=env,
                prompt=prompt,
                timeout=args.curator_timeout,
                log=step / "curator",
            )
        else:
            entry["curator"] = {"sec": 0.0, "exit": "skipped: no candidates"}
        entry["agent_sec"] = round(time.time() - t0, 1)
        agents = [*entry["reviewers"], entry["curator"]]
        entry["agent_exit"] = [a["exit"] for a in agents]
        entry["agent_output_tokens"] = sum(
            a.get("agent_output_tokens") or 0 for a in agents
        )
        entry["agent_turns"] = sum(a.get("agent_turns") or 0 for a in agents)
        entry["checks"] = _check_new_issues(kb_dir, before, pkg)
        entry["accepted"] = [c["issue"] for c in entry["checks"] if c.get("ok")]
        history.extend(records)
        log.append(entry)
        (out / "replay.json").write_text(
            json.dumps(log, indent=2) + "\n", encoding="utf-8"
        )
        print(
            f"[{index:02d}] {pkg.name}: kb {entry['kb_issues']} issues, "
            f"{len(entry['known_issues'])} known, {len(entry['leads'])} leads"
            + f", accepted {entry['accepted']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--trajectories", required=True, help="trajectories/<release> root"
    )
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--start-kb",
        type=Path,
        help="Start from this issue memory instead of the skill's generic checks.",
    )
    parser.add_argument("--order", default="date", choices=("date", "shuffle"))
    parser.add_argument("--shuffle-seed", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--only", nargs="*", help="Replay only these submissions, in order."
    )
    parser.add_argument("--repo", help="NIKA checkout for regression checks")
    parser.add_argument(
        "--agent-cmd", default="claude -p --permission-mode acceptEdits"
    )
    parser.add_argument("--agent-timeout", type=int, default=7200)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Continue a run in --out after its last finished step.",
    )
    parser.add_argument(
        "--packet-budget", type=int, default=10, help="Reviewer packets per reviewer."
    )
    parser.add_argument("--reviewers", type=int, default=4)
    parser.add_argument("--reviewer-turns", type=int, default=80)
    parser.add_argument("--curator-turns", type=int, default=120)
    parser.add_argument("--curator-timeout", type=int, default=3600)
    replay(parser.parse_args())


if __name__ == "__main__":
    main()
