"""Audit a trajectory package or benchmark result directory.

:func:`audit_trajectories` runs the deterministic layer of the submission
audit: every knowledge-base signature on every trial, the submission-level
metrics, a harness fingerprint, and a re-grade. It writes no files; the CLI
saves the returned report as ``audit.json`` and renders a Markdown summary.

The report separates **known issues** (``role: known``: root causes the
knowledge base already describes) from **leads** (``role: lead``: generic
checks that fired without a known cause). Leads are the agentic layer's
starting points; see the skill's SKILL.md.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

import yaml

from auditlib.cohort import TrialRecord, run_metrics
from auditlib.detectors import Hit, run_trial, tool_stats
from auditlib.kb import KnowledgeBase, Policy, load_kb
from auditlib.regrade import regrade_trial
from auditlib.trace import Trial, load_trial, split_tool_name, trial_dirs

SCHEMA = "nika-audit/1"
_FINGERPRINT_RUN_KEYS = (
    "agent_type",
    "model",
    "llm_provider",
    "sampling",
    "max_tokens",
    "nika_git_commit",
    "benchmark_version",
    "benchmark_split",
    "scoring_id",
)
_FINGERPRINT_INIT_KEYS = (
    "tools",
    "mcp_servers",
    "skills",
    "plugins",
    "memory_paths",
    "slash_commands",
    "claude_code_version",
    "model",
)


@dataclass
class AuditInput:
    trials: list[Trial]
    package: dict[str, Any]


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def read_package(root: Path) -> AuditInput:
    root = Path(root)
    identity = _read_yaml(root / "identity.yaml") or _read_yaml(
        root / "results" / "identity.yaml"
    )
    trials = [load_trial(path) for path in trial_dirs(root)]
    run = identity.get("run") or {}
    bench = identity.get("benchmark") or {}
    first = trials[0].run if trials else {}
    package = {
        "path": str(root),
        "slug": root.name,
        "release": bench.get("version") or first.get("benchmark_version"),
        "agent_type": run.get("agent_type") or first.get("agent_type"),
        "model": run.get("model") or first.get("model"),
        "nika_git_commit": run.get("nika_git_commit") or first.get("nika_git_commit"),
        "n_trials": len(trials),
    }
    return AuditInput(trials=trials, package=package)


def _canonical(value: Any) -> str:
    if isinstance(value, list):
        names = sorted(str(v.get("name") if isinstance(v, dict) else v) for v in value)
        return ", ".join(names)
    return str(value)


def fingerprint(trial: Trial, policy: Policy) -> dict[str, str]:
    """Harness settings of one trial.

    NIKA's own MCP servers and tools differ by scenario, so they are left out;
    what remains should be identical across the trials of a submission.
    """
    out = {f"run.{k}": _canonical(trial.run.get(k)) for k in _FINGERPRINT_RUN_KEYS}
    init = next((s for s in trial.steps if s.kind == "init"), None)
    if init is None:
        return out
    for key in _FINGERPRINT_INIT_KEYS:
        if key not in init.meta:
            continue
        value = init.meta[key]
        if key == "tools" and isinstance(value, list):
            value = [
                t
                for t in value
                if not policy.server_regex.fullmatch(split_tool_name(str(t))[0])
            ]
        elif key == "mcp_servers" and isinstance(value, list):
            value = [
                v
                for v in value
                if not policy.server_regex.fullmatch(
                    str(v.get("name") if isinstance(v, dict) else v)
                )
            ]
        out[f"init.{key}"] = _canonical(value)
    return out


def _record(
    trial: Trial, kb: KnowledgeBase, hits: list[Hit], source: str
) -> TrialRecord:
    preds = trial.submission.get("root_causes") or []
    anomaly = trial.submission.get("is_anomaly")
    mutating_ids = {
        i.id for i in kb.issues if any(s.kind == "mutation" for s in i.detect)
    }
    rca = trial.eval_metrics.get("rca_f1")
    return TrialRecord(
        trial_id=trial.trial_id,
        source=source,
        case_key=trial.case_key,
        scenario=trial.scenario,
        gt_types=sorted(
            {
                str(rc.get("fault_type"))
                for rc in trial.ground_truth.get("root_causes") or []
            }
        ),
        pred_types=sorted(
            {str(rc.get("fault_type")) for rc in preds if isinstance(rc, dict)}
        ),
        pred_anomaly=anomaly if isinstance(anomaly, bool) else None,
        rca_f1=float(rca) if isinstance(rca, (int, float)) else None,
        tool_calls=len(trial.calls()),
        mutating=any(h.issue in mutating_ids for h in hits),
        tools=tool_stats(trial, kb.policy),
    )


def _is_ancestor(commit: str, of: str, repo: Path | None) -> bool | None:
    if not commit or not of or repo is None:
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, of],
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode in (0, 1):
        return proc.returncode == 0
    return None


def _lift(
    issue_trials: set[str], records: list[TrialRecord], fault_types: set[str]
) -> dict[str, Any] | None:
    """Mean rca_f1 of trials that hit an issue vs. comparable trials that did not."""
    pool = [
        r
        for r in records
        if r.rca_f1 is not None and (not fault_types or set(r.gt_types) & fault_types)
    ]
    hit = [r.rca_f1 for r in pool if r.trial_id in issue_trials]
    miss = [r.rca_f1 for r in pool if r.trial_id not in issue_trials]
    if not hit or not miss:
        return None
    return {
        "rca_f1_hit": round(mean(hit), 3),
        "rca_f1_other": round(mean(miss), 3),
        "n_hit": len(hit),
        "n_other": len(miss),
    }


def audit_trajectories(
    root: Path,
    *,
    kb: KnowledgeBase | None = None,
    history: list[TrialRecord] | None = None,
    repo: Path | None = None,
    records_out: list[TrialRecord] | None = None,
) -> dict[str, Any]:
    """Run the deterministic audit over every trial under *root*.

    *records_out*, when given, receives the per-trial records, so a caller
    auditing submissions in order can pass them as later *history*.
    """
    kb = kb or load_kb()
    data = read_package(root)
    source = data.package["slug"]
    trial_hits: dict[str, list[Hit]] = {}
    records: list[TrialRecord] = []
    fingerprints: dict[str, dict[str, str]] = {}
    for trial in data.trials:
        hits = run_trial(kb, trial)
        trial_hits[trial.trial_id] = hits
        records.append(_record(trial, kb, hits, source))
        fingerprints[trial.trial_id] = fingerprint(trial, kb.policy)
    metric_hits = run_metrics(kb, records, history or [])
    if records_out is not None:
        records_out.extend(records)

    drift, drift_trials = _fingerprint_drift(fingerprints)
    contrast, contrast_trials = prompt_contrast(data.trials)
    drift_issues = [
        i for i in kb.issues if any(s.kind == "fingerprint_drift" for s in i.detect)
    ]
    by_issue: dict[str, set[str]] = defaultdict(set)
    for issue in drift_issues:
        by_issue[issue.id].update(drift_trials)
    contrast_issues = [
        i for i in kb.issues if any(s.kind == "prompt_contrast" for s in i.detect)
    ]
    for issue in contrast_issues:
        by_issue[issue.id].update(contrast_trials)
    for tid, hits in trial_hits.items():
        for hit in hits:
            by_issue[hit.issue].add(tid)
    for mh in metric_hits:
        by_issue[mh.issue].update(mh.trials)

    commit = str(data.package.get("nika_git_commit") or "")
    issues_out: list[dict[str, Any]] = []
    for issue in kb.issues:
        trials = by_issue.get(issue.id, set())
        metrics = [m.to_dict() for m in metric_hits if m.issue == issue.id]
        drifted = issue in drift_issues and bool(drift)
        contrasted = issue in contrast_issues and bool(contrast)
        if not trials and not metrics and not drifted and not contrasted:
            continue
        row: dict[str, Any] = {
            "id": issue.id,
            "class": issue.issue_class,
            "role": issue.role,
            "severity": issue.severity,
            "status": issue.status,
            "title": issue.title,
            "n_trials": len(trials),
            "trials": sorted(trials)[:20],
        }
        if metrics:
            row["metrics"] = metrics
        if drifted:
            row["drift"] = drift
        if contrasted:
            row["prompt_contrast"] = contrast
        if issue.status == "fixed" and issue.fixed_in:
            row["regression"] = _is_ancestor(issue.fixed_in, commit, repo)
        if issue.exploit or issue.issue_class == "shortcut":
            lift = _lift(trials, records, set(issue.scope.fault_types))
            if lift:
                row["lift"] = lift
        issues_out.append(row)

    exploited = _exploited(kb, by_issue, records)
    scores = _scores(records, exploited, data.trials)
    verdict = _verdict(issues_out)
    known = [r for r in issues_out if r["role"] == "known"]
    leads = [r for r in issues_out if r["role"] == "lead"]
    return {
        "schema": SCHEMA,
        "kb_issues": len(kb.issues),
        "package": data.package,
        "verdict": verdict,
        "summary": {
            "known_issues": len(known),
            "leads": len(leads),
            "trials_with_hits": sum(1 for h in trial_hits.values() if h),
            "unreadable_trace_lines": sum(t.bad_lines for t in data.trials),
        },
        "scores": scores,
        "known_issues": known,
        "leads": leads,
        "fingerprint": _fingerprint_summary(fingerprints),
        "trials": {
            tid: [h.to_dict() for h in hits]
            for tid, hits in sorted(trial_hits.items())
            if hits
        },
    }


def _exploited(
    kb: KnowledgeBase, by_issue: dict[str, set[str]], records: list[TrialRecord]
) -> set[str]:
    """Trials that scored after seeing a shortcut to their own fault.

    A trial counts when it hit an ``exploit`` issue, its ground truth is in
    the issue's fault-type scope (any, if unscoped), and it scored above 0.
    """
    by_id = {r.trial_id: r for r in records}
    out: set[str] = set()
    for issue in kb.issues:
        if not issue.exploit:
            continue
        scope = set(issue.scope.fault_types)
        for tid in by_issue.get(issue.id, set()):
            rec = by_id.get(tid)
            if rec is None or not rec.rca_f1:
                continue
            if scope and not scope & set(rec.gt_types):
                continue
            out.add(tid)
    return out


_DIGITS = re.compile(r"\d+")


def _prompt_lines(trial: Trial) -> set[str]:
    """Lines of the task prompt (the first prompt; later ones carry the report)."""
    prompt = next((s for s in trial.steps if s.kind == "prompt"), None)
    if prompt is None:
        return set()
    lines = {_DIGITS.sub("<n>", raw.strip()) for raw in prompt.text.splitlines()}
    return {line for line in lines if len(line) >= 8}


def prompt_contrast(
    trials: list[Trial], *, min_share: float = 0.5
) -> tuple[dict[str, list[str]], set[str]]:
    """Prompt lines that tell fault tasks from healthy ones, per environment.

    Healthy and fault tasks of one environment should get the same prompt.
    A line shared by at least *min_share* of the fault tasks and absent from
    every healthy task tells an agent that a fault exists. Returns the lines
    per environment and the fault trials that show them.
    """
    by_env: dict[str, list[tuple[Trial, set[str]]]] = defaultdict(list)
    for trial in trials:
        by_env[trial.scenario].append((trial, _prompt_lines(trial)))
    found: dict[str, list[str]] = {}
    hit_trials: set[str] = set()
    for env, rows in by_env.items():
        healthy = [lines for t, lines in rows if t.is_healthy_case]
        faulty = [(t, lines) for t, lines in rows if not t.is_healthy_case]
        if not healthy or not faulty:
            continue
        seen_healthy = set().union(*healthy)
        counts = Counter(line for _, lines in faulty for line in lines)
        variant = sorted(
            line
            for line, n in counts.items()
            if n >= min_share * len(faulty) and line not in seen_healthy
        )
        if variant:
            found[env] = variant
            hit_trials |= {t.trial_id for t, lines in faulty if lines & set(variant)}
    return found, hit_trials


def _fingerprint_drift(
    fps: dict[str, dict[str, str]],
) -> tuple[dict[str, dict[str, int]], set[str]]:
    """Fingerprint keys with more than one value, and the trials off the majority."""
    values: dict[str, Counter[str]] = defaultdict(Counter)
    for fp in fps.values():
        for key, value in fp.items():
            values[key][value] += 1
    drift = {k: dict(v) for k, v in values.items() if len(v) > 1}
    majority = {k: values[k].most_common(1)[0][0] for k in drift}
    minority = {
        tid for tid, fp in fps.items() if any(fp.get(k) != majority[k] for k in drift)
    }
    return drift, minority


def _fingerprint_summary(fps: dict[str, dict[str, str]]) -> dict[str, Any]:
    values: dict[str, Counter[str]] = defaultdict(Counter)
    for fp in fps.values():
        for key, value in fp.items():
            values[key][value] += 1
    return {
        k: (next(iter(v)) if len(v) == 1 else dict(v))
        for k, v in sorted(values.items())
    }


def _scores(
    records: list[TrialRecord], exploited: set[str], trials: list[Trial]
) -> dict[str, Any]:
    submitted = [r.rca_f1 for r in records if r.rca_f1 is not None]
    regraded: list[float] = []
    for trial in trials:
        rca = trial.eval_metrics.get("rca_f1")
        if not isinstance(rca, (int, float)):
            continue
        diff = next((d for d in regrade_trial(trial) if d["metric"] == "rca_f1"), None)
        regraded.append(
            diff["regraded"] if diff and diff["regraded"] is not None else float(rca)
        )
    audited = [
        0.0 if r.trial_id in exploited else r.rca_f1
        for r in records
        if r.rca_f1 is not None
    ]
    return {
        "rca_f1_submitted": round(mean(submitted), 4) if submitted else None,
        "rca_f1_regraded": round(mean(regraded), 4) if regraded else None,
        "rca_f1_audited": round(mean(audited), 4) if audited else None,
        "exploited_trials": len(exploited),
    }


def _verdict(issues: list[dict[str, Any]]) -> str:
    """``block`` on a block-severity issue that is open or recurs after its fix.

    A block-severity issue already fixed after the submission's commit (or
    whose fix cannot be placed) only warns: the submission predates the fix.
    """
    for r in issues:
        if r["severity"] != "block":
            continue
        if r["status"] != "fixed" or r.get("regression") is True:
            return "block"
    if any(r["severity"] in ("block", "warn") for r in issues):
        return "warn"
    return "pass"


def render_markdown(report: dict[str, Any], *, max_rows: int = 30) -> str:
    """Short PR-comment summary of an audit report."""
    pkg = report["package"]
    s = report["scores"]
    lines = [
        f"## NIKA submission audit: `{pkg['slug']}`",
        "",
        f"Verdict: **{report['verdict'].upper()}** · {pkg['n_trials']} trials · "
        f"knowledge base of {report['kb_issues']} issues",
        "",
        "| Score | rca_f1 |",
        "| --- | --- |",
        f"| Submitted | {s['rca_f1_submitted']} |",
        f"| Re-graded | {s['rca_f1_regraded']} |",
        f"| Audited (shortcut trials scored 0) | {s['rca_f1_audited']} |",
        "",
    ]

    def table(title: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        lines.extend(
            [
                f"### {title}",
                "",
                "| Issue | Class | Severity | Trials | Note |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for row in rows[:max_rows]:
            note = []
            if row.get("regression"):
                note.append("regression after fix")
            if row.get("lift"):
                lift = row["lift"]
                note.append(f"rca {lift['rca_f1_hit']} vs {lift['rca_f1_other']}")
            for m in row.get("metrics", [])[:2]:
                note.append(f"{m['metric']}={m['value']} @ {m['group']}")
            lines.append(
                f"| `{row['id']}` | {row['class']} | {row['severity']} | {row['n_trials']} | {'; '.join(note)} |"
            )
        if len(rows) > max_rows:
            lines.append(f"\n{len(rows) - max_rows} more in audit.json.")
        lines.append("")

    order = {"block": 0, "warn": 1, "info": 2}
    table(
        "Known issues",
        sorted(
            report["known_issues"], key=lambda r: (order[r["severity"]], -r["n_trials"])
        ),
    )
    table(
        "Leads for review",
        sorted(report["leads"], key=lambda r: (order[r["severity"]], -r["n_trials"])),
    )
    return "\n".join(lines)


def write_report(report: dict[str, Any], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
