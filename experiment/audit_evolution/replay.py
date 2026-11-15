"""Replay evaluation runs through one experiment arm.

Arms:
  ours  guideline + growing knowledge base (records, signatures, lessons), all auto-accepted
  b1    guideline, empty memory for every run
  b2    guideline, empty knowledge base, raw findings of earlier runs appended to the prompt
  b3    open-ended agent audit with the ABC checklist, no class questions, tools, or memory

Each step audits one evaluation run in <ROOT>/runs/<bench>/<arm>/s<seed>/<NN>_<run>/.
Ours and b2 depend on the previous step; b1 and b3 steps are independent.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

HERE = Path(__file__).resolve().parent
SEQUENTIAL = {"ours", "b2"}
RAW_HISTORY_CHARS = 120_000  # about 30k tokens of raw findings for b2


def step_dir(bench: str, arm: str, seed: int, i: int, run: str) -> Path:
    return C.RUNS / bench / arm / f"s{seed}" / f"{i:02d}_{run}"


def history_dir(bench: str, work: Path, earlier: list[str]) -> Path | None:
    """Leaderboard-style scores of the runs audited before this one.

    Built from the audited dataset's own trial files, so planted grading or label
    defects appear in the history as they do in the trials.
    """
    if not earlier:
        return None
    hist = work / "history"
    for run in earlier:
        out = hist / run / "results" / "trials"
        if out.exists():
            continue
        for t in sorted((C.benchmark_dir(bench) / run / "trials").iterdir()):
            try:
                rj = json.loads((t / "run.json").read_text())
                gt = json.loads((t / "ground_truth.json").read_text())
                sub = json.loads((t / "submission.json").read_text())
                metrics = json.loads((t / "eval_metrics.json").read_text())
            except (OSError, json.JSONDecodeError):
                continue
            pred = [
                r.get("fault_type")
                for r in sub.get("root_causes") or []
                if isinstance(r, dict)
            ]
            C.write_json(
                out / t.name / "result.json",
                {
                    "trial_id": t.name,
                    "case_key": rj.get("case_key"),
                    "scenario": rj.get("scenario_name"),
                    "gt_fault_types": [
                        r.get("fault_type") for r in gt.get("root_causes") or []
                    ],
                    "predicted_fault_types": pred or None,
                    "metrics": metrics,
                },
            )
    return hist


def raw_findings(review: Path) -> str:
    """Groups the adjudicator judged real, as unprocessed text (b2 memory)."""
    try:
        groups = json.loads((review / "groups.json").read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    lines = []
    for g in groups:
        if g.get("verdict") == "real":
            excerpt = (g.get("excerpts") or [""])[0]
            lines.append(
                f"- [{g.get('class')}] {g.get('claim')} | excerpt: {str(excerpt)[:300]} | {', '.join((g.get('refs') or [])[:3])}"
            )
    return "\n".join(lines)


def kb_index(kb: Path) -> str:
    import yaml

    rows = []
    for f in sorted((kb / "issues").glob("*.yaml")):
        d = yaml.safe_load(f.read_text())
        scope = d.get("scope") or {}
        rows.append(
            f"- {d['id']} [{d.get('class')}] {d.get('title')}: {d.get('summary', '')}"
            + (
                f" (scenarios: {', '.join(scope.get('scenarios', []))})"
                if scope.get("scenarios")
                else ""
            )
        )
    return "\n".join(rows)


CODEX_HOME = (
    C.ROOT / "codex-sub"
)  # copy of the user's ChatGPT login (authorized 2026-11-15)
NVM = Path.home() / ".nvm"
NODE_BIN = NVM / "versions" / "node" / "v24.21.0" / "bin"


def run_agent(
    work: Path,
    prompt: str,
    alias: str,
    readonly: list[Path],
    budget: int | None,
    max_turns: int,
) -> dict:
    """The audit agent: Codex for ``openai/<model>`` aliases, Claude Code otherwise."""
    (work / "prompt.md").write_text(prompt)
    if alias.startswith("openai/"):
        return run_codex(work, prompt, alias.removeprefix("openai/"), readonly, budget)
    return run_claude(work, prompt, alias, readonly, budget, max_turns)


def run_codex(
    work: Path, prompt: str, model: str, readonly: list[Path], budget: int | None
) -> dict:
    """Codex inside bwrap with the ChatGPT login, as in the leaderboard CI; JSON events in agent.jsonl."""
    env = {
        "CODEX_HOME": str(CODEX_HOME),
        "PATH": f"{NODE_BIN}:{C.PYTHON.parent}:/usr/local/bin:/usr/bin:/bin",
    }
    cmd = C.bwrap([*readonly, NVM], [work, CODEX_HOME], env) + [
        "--chdir",
        str(work),
        "codex",
        "exec",
        "--ignore-user-config",
        "--ephemeral",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "--model",
        model,
        "--json",
        "-C",
        str(work),
        "-",
    ]
    used = {"input": 0, "output": 0, "cache_read": 0}
    stopped, api_error = None, None
    t0 = time.time()
    with (
        open(work / "agent.jsonl", "w") as log,
        subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=open(work / "agent.err", "w"),
            text=True,
        ) as p,
    ):
        p.stdin.write(prompt)
        p.stdin.close()
        for line in p.stdout:
            log.write(line)
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "turn.completed":
                u = ev.get("usage") or {}
                used["input"] += u.get("input_tokens", 0)
                used["cache_read"] += u.get("cached_input_tokens", 0)
                used["output"] += u.get("output_tokens", 0)
            elif ev.get("type") in ("turn.failed", "error"):
                api_error = str(ev.get("error") or ev.get("message"))[:500]
            if budget and used["input"] + used["output"] > budget:
                stopped = "budget"
                p.terminate()
                break
        rc = p.wait()
    return {
        "returncode": rc,
        "seconds": round(time.time() - t0),
        "tokens": used,
        "stopped": stopped,
        "api_error": api_error,
    }


def run_claude(
    work: Path,
    prompt: str,
    alias: str,
    readonly: list[Path],
    budget: int | None,
    max_turns: int,
) -> dict:
    """Claude Code inside bwrap; stream-json log in agent.jsonl. Stops at the token budget."""
    cfg = work / ".claude-config"
    cfg.mkdir(exist_ok=True)
    (cfg / "settings.json").write_text(
        json.dumps(
            {
                "permissions": {"defaultMode": "bypassPermissions"},
                "skipDangerousModePermissionPrompt": True,
            }
        )
    )
    env = C.llm_env(alias) | {
        "CLAUDE_CONFIG_DIR": str(cfg),
        # Keep the agent's turns on one replica so they reuse its prefix cache.
        "ANTHROPIC_CUSTOM_HEADERS": f"X-Gateway-Session: audit-evo-agent-{work.parent.parent.name}-{work.parent.name}-{work.name}"[
            :200
        ],
    }
    cmd = C.bwrap(readonly, [work], env) + [
        "--chdir",
        str(work),
        "claude",
        "-p",
        prompt,
        "--model",
        alias,
        "--output-format",
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--max-turns",
        str(max_turns),
        "--strict-mcp-config",
    ]
    per_msg: dict[
        str, dict
    ] = {}  # stream-json repeats a message's usage per content block
    used = {"input": 0, "output": 0, "cache_read": 0}
    stopped = None
    t0 = time.time()
    with (
        open(work / "agent.jsonl", "w") as log,
        subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=open(work / "agent.err", "w"), text=True
        ) as p,
    ):
        for line in p.stdout:
            log.write(line)
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "assistant" and (msg.get("message") or {}).get(
                "usage"
            ):
                per_msg[msg["message"].get("id", len(per_msg))] = msg["message"][
                    "usage"
                ]
                used = {
                    "input": sum(u.get("input_tokens", 0) for u in per_msg.values()),
                    "output": sum(u.get("output_tokens", 0) for u in per_msg.values()),
                    "cache_read": sum(
                        u.get("cache_read_input_tokens", 0) for u in per_msg.values()
                    ),
                }
            if msg.get("type") == "result":
                for u in (
                    msg.get("model_usage_since_last_result")
                    or msg.get("modelUsage")
                    or {}
                ).values():
                    used = {
                        "input": u.get("inputTokens", 0),
                        "output": u.get("outputTokens", 0),
                        "cache_read": u.get("cacheReadInputTokens", 0),
                    }
            if budget and used["input"] + used["output"] + used["cache_read"] > budget:
                stopped = "budget"
                p.terminate()
                break
        rc = p.wait()
    return {
        "returncode": rc,
        "seconds": round(time.time() - t0),
        "tokens": used,
        "stopped": stopped,
    }


def check_agent(work: Path, record: dict, output: str) -> None:
    """Fail the step if the agent hit API errors or left no output file.

    An agent cut off by an unavailable endpoint would otherwise pass for one that
    found nothing; a stopped budget is a normal end for b3.
    """
    result = {}
    for line in (work / "agent.jsonl").read_text().splitlines():
        if '"type":"result"' in line or '"type": "result"' in line:
            result = json.loads(line)
    # Hitting the turn limit is a normal end; the decisions written so far count.
    turn_limit = result.get("subtype") == "error_max_turns"
    api_error = (
        (result.get("is_error") and not turn_limit)
        or result.get("api_error_status")
        or record["agent"].get("api_error")
    )
    if record["agent"].get("stopped") == "budget":
        api_error = False
    if api_error or not (work / output).exists():
        record["agent"]["failure"] = {
            "api_error": api_error,
            "missing": None if (work / output).exists() else output,
        }
        C.write_json(work / "failed.json", record)
        raise RuntimeError(f"agent failed: {work}")


def pipeline_tokens(review: Path) -> dict:
    """Tokens of the scanner and adjudicator calls made in this step (cache hits cost none)."""
    tot = {"input": 0, "output": 0}
    calls = review / "scan-calls.jsonl"
    if calls.exists():
        for line in calls.open():
            r = json.loads(line)
            if not r.get("cached"):
                tot["input"] += r.get("prompt_tokens") or 0
                tot["output"] += r.get("completion_tokens") or 0
    verdicts = review / "verdicts-calls.jsonl"
    if verdicts.exists():
        for line in verdicts.open():
            res = json.loads(line).get("result") or {}
            if res.get("cached"):
                continue
            t = res.get("tokens") or [0, 0]
            tot["input"] += t[0]
            tot["output"] += t[1]
    return tot


def do_step(a, i: int, run: str, earlier: list[str]) -> Path:
    work = step_dir(a.bench, a.arm, a.seed, i, run)
    if (work / "done.json").exists():
        return work
    work.mkdir(parents=True, exist_ok=True)
    traj = C.benchmark_dir(a.bench) / run
    prev = step_dir(a.bench, a.arm, a.seed, i - 1, earlier[-1]) if earlier else None
    record = {
        "bench": a.bench,
        "arm": a.arm,
        "seed": a.seed,
        "index": i,
        "run": run,
        "alias": a.alias,
    }

    if a.arm == "b3":
        prompt = (
            (HERE / "prompts" / "open_audit.md")
            .read_text()
            .format(
                trajectories=traj,
                repo=C.REPO,
                work=work,
                classes=", ".join(taxonomy_ids()),
            )
        )
        record["agent"] = run_agent(
            work, prompt, a.alias, [C.TREE, C.REPO, traj], a.budget, a.max_turns
        )
        check_agent(work, record, "findings.json")
        C.write_json(work / "done.json", record)
        return work

    # Memory carried into this step.
    kb_in = (prev / "review" / "kb") if (a.arm == "ours" and prev) else C.EMPTY_KB
    lessons = work / "lessons.md"
    if not lessons.exists():
        if a.arm == "ours" and prev and (prev / "lessons.md").exists():
            shutil.copy(prev / "lessons.md", lessons)
        else:
            lessons.write_text("# Audit lessons\n")
    hist = history_dir(a.bench, work, earlier) if a.arm in SEQUENTIAL else None

    review = work / "review"
    if not (review / "summary.json").exists():
        cmd = [
            str(C.PYTHON),
            str(HERE / "stage_review.py"),
            str(traj),
            "--workdir",
            str(review),
            "--kb",
            str(kb_in),
            "--repo",
            str(C.REPO),
            "--samples",
            "3",
            "--workers",
            str(a.workers),
            "--judge-workers",
            str(a.judge_workers),
        ]
        if hist:
            cmd += ["--history", str(hist)]
        env = C.llm_env(a.alias) | {
            "PATH": "/usr/bin:/bin",
        }
        t0 = time.time()
        with open(work / "review.log", "w") as log:
            rc = subprocess.run(
                cmd, env=env, stdout=log, stderr=subprocess.STDOUT
            ).returncode
        record["review"] = {"returncode": rc, "seconds": round(time.time() - t0)}
        if rc:
            C.write_json(work / "failed.json", record)
            raise RuntimeError(f"review failed: {work}")
        shutil.copytree(review / "kb", work / "kb.pipeline", dirs_exist_ok=True)

    before = {f.name for f in (kb_in / "issues").glob("*.yaml")}
    proposals = sorted(
        f.name
        for f in (review / "kb" / "issues").glob("*.yaml")
        if f.name not in before
    )
    memory = ""
    if a.arm == "ours":
        memory = (
            "\nKnowledge base before this run (accepted records):\n"
            + (kb_index(kb_in) or "(empty)")
            + "\n\nLessons from earlier audits:\n"
            + lessons.read_text()
        )
    elif a.arm == "b2":
        raw = []
        for j, r in enumerate(earlier):
            raw.append(raw_findings(step_dir(a.bench, a.arm, a.seed, j, r) / "review"))
        text = "\n".join(x for x in reversed(raw) if x)[:RAW_HISTORY_CHARS]
        memory = "\nRaw findings from earlier audits (unreviewed, newest first):\n" + (
            text or "(none)"
        )
    prompt = (
        (HERE / "prompts" / "verify.md")
        .read_text()
        .format(
            skill=C.SKILL,
            trajectories=traj,
            review=review,
            repo=C.REPO,
            lessons=lessons,
            work=work,
            proposals=", ".join(proposals) or "(none)",
            memory=memory,
        )
    )
    # A retried step starts the agent again from the pipeline's KB and the carried lessons.
    shutil.rmtree(review / "kb")
    shutil.copytree(work / "kb.pipeline", review / "kb")
    if (work / "lessons.in.md").exists():
        shutil.copy(work / "lessons.in.md", lessons)
    else:
        shutil.copy(lessons, work / "lessons.in.md")
    record["agent"] = run_agent(
        work,
        prompt,
        a.alias,
        [C.TREE, C.REPO, traj],
        a.verify_budget or None,
        a.max_turns,
    )
    check_agent(work, record, "verify.json")
    record["pipeline_tokens"] = pipeline_tokens(review)
    record["proposals"] = proposals
    # Auto-accept: the agent's edited KB is the next step's input. Fall back if it broke the KB.
    ok = (
        subprocess.run(
            [
                str(C.PYTHON),
                "-m",
                "nika.audit.cli",
                "kb-validate",
                "--kb",
                str(review / "kb"),
            ],
            capture_output=True,
        ).returncode
        == 0
    )
    if not ok:
        shutil.rmtree(review / "kb")
        shutil.copytree(work / "kb.pipeline", review / "kb")
        record["kb_restored"] = True
    # Matches of the accepted KB on this run, the basis for scoring.
    cmd = [
        str(C.PYTHON),
        "-m",
        "nika.audit.cli",
        "run",
        str(traj),
        "--kb",
        str(review / "kb"),
        "--repo",
        str(C.REPO),
        "--out",
        str(work / "audit-final.json"),
    ]
    if hist:
        cmd += ["--history", str(hist)]
    subprocess.run(
        cmd, stdout=subprocess.DEVNULL, stderr=open(work / "audit-final.err", "w")
    )
    C.write_json(work / "done.json", record)
    return work


def taxonomy_ids() -> list[str]:
    import yaml

    data = yaml.safe_load((C.EMPTY_KB / "taxonomy.yaml").read_text())
    items = data.get("classes", data) if isinstance(data, dict) else data
    return [c["id"] for c in items]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", default="nika-0.2.0")
    ap.add_argument("--arm", choices=["ours", "b1", "b2", "b3"], required=True)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--alias", required=True, help="Gateway model alias.")
    ap.add_argument(
        "--workers", type=int, default=16, help="Parallel LLM calls per review."
    )
    ap.add_argument("--judge-workers", type=int, default=32)
    ap.add_argument(
        "--verify-budget",
        type=int,
        default=8_000_000,
        help="Token cap per Verify agent.",
    )
    ap.add_argument("--jobs", type=int, default=1, help="Parallel steps for b1 and b3.")
    ap.add_argument("--limit", type=int, help="Audit only the first N runs.")
    ap.add_argument("--budget", type=int, help="Token budget per step (b3).")
    ap.add_argument("--max-turns", type=int, default=300)
    a = ap.parse_args()
    names = C.order(a.bench, a.seed)[: a.limit]
    if a.arm in SEQUENTIAL:
        for i, run in enumerate(names):
            print(do_step(a, i, run, names[:i]), flush=True)
    else:
        with ThreadPoolExecutor(a.jobs) as ex:
            for w in ex.map(lambda x: do_step(a, x[0], x[1], []), enumerate(names)):
                print(w, flush=True)


if __name__ == "__main__":
    main()
