"""Find cross-trial issues: read per-environment tables, then record statistics.

Some issues show only across trials: a fault every configuration fails, a
label always answered as another, a healthy environment most agents call
faulty, a tool that errors or truncates on many calls. A single trace cannot
show them. This module builds, per environment, a table of cases (scores,
answers, fault-absent and state-change counts, over this submission and the
earlier ones), healthy tasks, and tools; an LLM reads each table with the
issue-class questions and cites rows; each cited row is adjudicated; and a
real finding becomes an issue with the matching statistical signature, scoped
to the row's fault type, environment, or tool (``nika.audit.cohort`` metrics).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from nika.audit.cohort import TrialRecord, run_metrics
from nika.audit.curate import PREFIX
from nika.audit.kb import Issue, KnowledgeBase, Scope, Signature
from nika.audit.runner import (
    _DIGITS,
    _prompt_lines,
    _prompt_min_support,
    prompt_contrast,
    prompt_profiles,
)
from nika.audit.scan import FINDINGS_FORMAT, ScanConfig, _NO_STRUCTURED, chat
from nika.audit.trace import Trial

_JSON = re.compile(r"\{.*\}", re.S)
FAULT_ABSENT_CHECK = "VALIDITY-FAULT-ABSENT-AT-CLEANUP"


@dataclass
class Row:
    id: str
    kind: str  # case | healthy | tool | prompt
    env: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)


def _top(counter: Counter, n: int = 4) -> str:
    return ", ".join(f"{k} x{v}" for k, v in counter.most_common(n)) or "none"


def prompt_rows(trials: list[Trial]) -> list[Row]:
    """Expose sentence-level evidence for deployment-matched prompt contrasts."""
    rows = []
    for group in prompt_profiles(trials):
        found, _ = prompt_contrast(group)
        prompts = [
            (t, next(s for s in t.steps if s.kind == "prompt"))
            for t in group
            if _prompt_lines(t)
        ]
        faults = [(t, s) for t, s in prompts if not t.is_healthy_case]
        healthy = [(t, s) for t, s in prompts if t.is_healthy_case]
        if not faults or not healthy:
            continue
        for line in found.get(group[0].scenario, []):
            t, source = next(
                (t, s)
                for t, s in faults
                if line
                in {_DIGITS.sub("<n>", raw.strip()) for raw in s.text.splitlines()}
            )
            raw = next(
                raw.strip()
                for raw in source.text.splitlines()
                if _DIGITS.sub("<n>", raw.strip()) == line
            )
            h, peer = healthy[0]
            for fragment in re.split(r"(?<=[.!?])\s+", raw):
                n_fault = sum(fragment in s.text for _, s in faults)
                n_healthy = sum(fragment in s.text for _, s in healthy)
                normalized = _DIGITS.sub("<n>", fragment)
                if (
                    len(fragment) < 8
                    or n_fault < _prompt_min_support(len(faults))
                    or any(normalized in _DIGITS.sub("<n>", s.text) for _, s in healthy)
                ):
                    continue
                evidence = {
                    "environment": t.scenario,
                    "fault_trials": len(faults),
                    "healthy_trials": len(healthy),
                    "fault_occurrences": n_fault,
                    "healthy_occurrences": n_healthy,
                    "fragment": fragment,
                    "fault_prompt_contains_fragment": fragment in source.text,
                    "healthy_prompt_contains_fragment": fragment in peer.text,
                    "fault_source": f"{t.trial_id}:{source.index}",
                    "healthy_source": f"{h.trial_id}:{peer.index}",
                    "fault_prompt": source.text,
                    "healthy_prompt": peer.text,
                    "matched_deployment_and_commit": True,
                    "commit": t.run.get("nika_git_commit"),
                }
                key = json.dumps(
                    [evidence["fault_source"], evidence["healthy_source"], fragment]
                )
                row_id = "prompt:" + hashlib.sha256(key.encode()).hexdigest()[:16]
                text = (
                    f"[{row_id}] Prompt fragment: {fragment}\n"
                    f"Fault tasks: {n_fault}/{len(faults)} contain this exact fragment. "
                    f"Healthy tasks: {n_healthy}/{len(healthy)} contain this exact fragment.\n"
                    + json.dumps(evidence)
                )
                rows.append(
                    Row(
                        row_id,
                        "prompt",
                        t.scenario,
                        text,
                        {
                            "pattern": re.escape(line),
                            "scenario": t.scenario,
                            "refs": [
                                evidence["fault_source"],
                                evidence["healthy_source"],
                            ],
                            "prompt_observation": {
                                k: evidence[k]
                                for k in (
                                    "fragment",
                                    "fault_occurrences",
                                    "fault_trials",
                                    "healthy_occurrences",
                                    "healthy_trials",
                                )
                            },
                        },
                    )
                )
    return rows


def build_rows(
    current: list[TrialRecord],
    history: list[TrialRecord],
    report: dict[str, Any],
) -> list[Row]:
    """Table rows for one submission; pooled counts include *history*."""
    absent = Counter()
    for tid, hits in (report.get("trials") or {}).items():
        if any(h.get("issue") == FAULT_ABSENT_CHECK for h in hits):
            absent[tid] += 1
    pool = defaultdict(list)
    for r in [*current, *history]:
        pool[r.case_key].append(r)
    rows: list[Row] = []
    by_case = defaultdict(list)
    for r in current:
        by_case[r.case_key].append(r)
    for case, recs in sorted(by_case.items()):
        env = recs[0].scenario
        if not recs[0].gt_types:
            continue
        everyone = pool[case]
        scored = [r for r in everyone if r.rca_f1 is not None]
        zero = sum((r.rca_f1 or 0) == 0 for r in scored)
        answers = Counter(t for r in everyone for t in r.pred_types)
        wrong = Counter(
            t for r in everyone for t in r.pred_types if t not in r.gt_types
        )
        here = [r.rca_f1 for r in recs if r.rca_f1 is not None]
        text = (
            f"[case:{case}] fault {', '.join(recs[0].gt_types)}; this submission: "
            f"{len(recs)} trials, mean rca_f1 "
            f"{(sum(here) / len(here)) if here else 0:.2f}; all submissions so far: "
            f"{len(everyone)} trials from {len({r.source for r in everyone})} "
            f"configurations, {zero / max(1, len(scored)):.0%} scored 0; answers: "
            f"{_top(answers)}; fault absent at cleanup here: "
            f"{sum(absent[r.trial_id] for r in recs)}; state-changing commands here: "
            f"{sum(r.mutating for r in recs)}"
        )
        rows.append(
            Row(
                f"case:{case}",
                "case",
                env,
                text,
                {
                    "fault_types": list(recs[0].gt_types),
                    "confused_with": wrong.most_common(1)[0][0] if wrong else "",
                    "fault_absent": sum(absent[r.trial_id] for r in recs),
                },
            )
        )
    healthy_pool = defaultdict(list)
    for r in [*current, *history]:
        if not r.gt_types:
            healthy_pool[r.scenario].append(r)
    for env in sorted({r.scenario for r in current if not r.gt_types}):
        recs = [r for r in current if r.scenario == env and not r.gt_types]
        everyone = healthy_pool[env]
        flagged = [r for r in everyone if r.pred_anomaly or r.pred_types]
        text = (
            f"[healthy:{env}] healthy tasks; this submission: {len(recs)} trials, "
            f"{sum(bool(r.pred_anomaly or r.pred_types) for r in recs)} reported a "
            f"fault; all submissions so far: {len(everyone)} trials, "
            f"{len(flagged) / max(1, len(everyone)):.0%} reported a fault; labels "
            f"given: {_top(Counter(t for r in everyone for t in r.pred_types))}"
        )
        rows.append(Row(f"healthy:{env}", "healthy", env, text, {"scenario": env}))
    tools: dict[str, Counter] = defaultdict(Counter)
    for r in current:
        for tool, row in r.tools.items():
            tools[tool].update(row)
    for tool, c in sorted(tools.items()):
        if c["calls"] < 5:
            continue
        text = (
            f"[tool:{tool}] {c['calls']} calls in this submission, "
            f"{c['errors'] / c['calls']:.0%} errors, "
            f"{c['truncated'] / c['calls']:.0%} truncated"
        )
        rows.append(
            Row(
                f"tool:{tool}",
                "tool",
                "",
                text,
                {
                    "tool": tool,
                    "error_rate": c["errors"] / c["calls"],
                    "truncation_rate": c["truncated"] / c["calls"],
                },
            )
        )
    return rows


SCAN_SYSTEM = """\
You audit NIKA, a benchmark for network troubleshooting agents, from \
statistics over many recorded runs ("trials") of one leaderboard submission and \
the submissions before it. Each table row summarises one fault case, the \
healthy (fault-free) tasks of an environment, or one tool. You look for \
problems with the benchmark, its harness and tools, or the runs, that only \
show across trials, using the questions below.

Questions, one per issue class:
{questions}

Rules:
* Report every row whose numbers may answer a question, and say why. Typical \
signs: a fault almost every configuration scores 0 on; one wrong label given \
for a fault far more often than the right one; healthy tasks that most \
trials report as faulty; a tool with many errors or truncated outputs; faults \
often absent at cleanup or state-changing commands. Do not dismiss a row \
because it could be the agents' weakness; another step judges it.
* Cite the row id and copy the numbers you rely on character for character.
* Report nothing for rows that look normal.

Reply with JSON only:
{{"findings": [{{"class": "<class id>", "row": "<row id>", "claim": "<one \
sentence>", "excerpt": "<verbatim part of the row>"}}]}}"""

JUDGE_VERDICTS = ("real", "by_design", "agent_weakness", "unclear")
JUDGE_SYSTEM = """\
You review one candidate problem that a scan of cross-trial statistics \
reported for NIKA, a benchmark for network troubleshooting agents. Decide \
whether the statistics show a problem with the benchmark, its harness and \
tools, or the runs, rather than ordinary agent weakness.

Issue classes:
{classes}

Verdicts:
- real: the numbers show a benchmark, harness, tool, or run problem as the \
class describes (for example a fault no configuration can solve because it \
leaves no symptom, labels agents cannot tell apart, a lure in a healthy \
environment, a tool that fails or truncates often).
- by_design: the numbers are what the design intends and would not mislead.
- agent_weakness: the numbers reflect weak agents on a solvable task.
- unclear: the statistics shown cannot settle it.

Reply with JSON only: {{"verdict": "<real|by_design|agent_weakness|unclear>", \
"reason": "<one or two sentences>"}}"""


def _parse(content: str) -> dict:
    match = _JSON.search(content or "")
    try:
        return json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        return {}


def _render(rows: list[Row]) -> str:
    return "\n".join(r.text for r in rows)


class _Responses:
    """Persist valid exact-packet replies before another table finishes."""

    def __init__(self, path: Path | None):
        self.path = path
        self.data = json.loads(path.read_text()) if path and path.is_file() else {}
        self.lock = threading.Lock()

    def key(self, cfg: ScanConfig, system: str, user: str) -> str:
        return hashlib.sha256(
            json.dumps(
                [
                    cfg.model,
                    cfg.command,
                    cfg.thinking,
                    cfg.temperature,
                    cfg.max_tokens,
                    system,
                    user,
                ]
            ).encode()
        ).hexdigest()

    def get(self, key: str) -> dict | None:
        result = self.data.get(key)
        if not isinstance(result, dict) or result.get("error"):
            return None
        return {
            **result,
            "cached": True,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "http_attempts": 0,
            "sec": 0,
        }

    def put(self, key: str, result: dict) -> None:
        with self.lock:
            self.data[key] = result
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix(".tmp")
                temporary.write_text(json.dumps(self.data) + "\n")
                temporary.replace(self.path)

    def record(self, key: str, result: dict) -> None:
        if self.path:
            with self.lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                journal = self.path.with_name(self.path.stem + "-calls.jsonl")
                with journal.open("a") as stream:
                    stream.write(json.dumps({"key": key, "result": result}) + "\n")


def _scan_error(result: dict) -> str:
    data = _parse(result.get("content", ""))
    if result.get("error"):
        return result["error"]
    if result.get("finish") != "stop":
        return "incomplete cohort scan response"
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        return "invalid cohort scan response"
    return ""


def _format(rows: list[Row]) -> dict:
    result = deepcopy(FINDINGS_FORMAT)
    result["json_schema"]["name"] = "audit_cohort_findings_bounded"
    item = result["json_schema"]["schema"]["properties"]["findings"]["items"]
    item["properties"] = {
        "class": {"type": "string", "maxLength": 80},
        "row": {"type": "string", "enum": [r.id for r in rows]},
        "claim": {"type": "string", "maxLength": 600},
        "excerpt": {"type": "string", "maxLength": 500},
    }
    item["required"] = ["class", "row", "claim", "excerpt"]
    return result


def scan_rows(
    rows: list[Row],
    kb: KnowledgeBase,
    cfg: ScanConfig,
    *,
    calls_out: list | None = None,
    cache_path: Path | None = None,
) -> list[dict[str, Any]]:
    """One call per environment (and one for tools); verified findings only."""
    questions = "\n".join(f"- {c.id}: {c.question}" for c in kb.taxonomy)
    system = SCAN_SYSTEM.format(questions=questions)
    tables = defaultdict(list)
    for r in rows:
        tables[r.env or "tools"].append(r)
    by_id = {r.id: r for r in rows}
    cache = _Responses(cache_path)

    def one(env: str) -> list[dict[str, Any]]:
        user = f"Table for {env}:\n{_render(tables[env])}"
        key = cache.key(cfg, system, user)
        result = cache.get(key)
        if result is None or _scan_error(result):
            result = chat(client, cfg, system, user)
            error = _scan_error(result)
            attempts = []
            if (
                error
                in {
                    "invalid cohort scan response",
                    "incomplete cohort scan response",
                }
                and cfg.retries
            ):
                attempts.append({**result, "error": error})
                endpoint = (cfg.base_url, cfg.model)
                structured = not cfg.command and endpoint not in _NO_STRUCTURED
                result = chat(
                    client,
                    cfg,
                    system,
                    user,
                    **({"response_format": _format(tables[env])} if structured else {}),
                )
                if structured and result.get("status_code") in {400, 422}:
                    _NO_STRUCTURED.add(endpoint)
                    attempts.append(result)
                    result = chat(client, cfg, system, user)
                error = _scan_error(result)
            if error:
                result["error"] = error
            if attempts:
                attempts.append(result)
                result = {
                    **result,
                    "attempts": attempts,
                    **{
                        k: sum(a.get(k) or 0 for a in attempts)
                        for k in (
                            "prompt_tokens",
                            "completion_tokens",
                            "http_attempts",
                            "sec",
                        )
                    },
                }
            cache.record(key, result)
            if not error:
                cache.put(key, result)
        if calls_out is not None:
            calls_out.append({"environment": env, **result})
        if result.get("error"):
            return []
        data = _parse(result.get("content", ""))
        out = []
        for f in data["findings"]:
            if not isinstance(f, dict):
                continue
            row = by_id.get(str(f.get("row", "")).strip("[]"))
            excerpt = str(f.get("excerpt") or "")
            if row is None or not excerpt.strip() or excerpt.strip() not in row.text:
                continue
            out.append({**f, "row": row.id})
        return out

    with httpx.Client(limits=httpx.Limits(max_connections=cfg.workers)) as client:
        with ThreadPoolExecutor(cfg.workers) as pool:
            return [f for found in pool.map(one, sorted(tables)) for f in found]


def judge(
    findings: list[dict[str, Any]],
    rows: list[Row],
    kb: KnowledgeBase,
    cfg: ScanConfig,
    *,
    cache_path: Path | None = None,
) -> list[dict[str, Any]]:
    classes = "\n".join(f"- {c.id}: {c.question}" for c in kb.taxonomy)
    system = JUDGE_SYSTEM.format(classes=classes)
    by_id = {r.id: r for r in rows}
    cache = _Responses(cache_path)

    def one(f: dict[str, Any]) -> dict[str, str]:
        user = (
            f"Class: {f.get('class')}\nClaim: {f.get('claim')}\n"
            f"Row: {by_id[f['row']].text}"
        )
        key = cache.key(cfg, system, user)
        result = cache.get(key) or chat(client, cfg, system, user)
        if not result.get("cached"):
            cache.record(key, result)
        data = _parse(result.get("content", ""))
        verdict = data.get("verdict") if isinstance(data, dict) else None
        reason = data.get("reason") if isinstance(data, dict) else None
        valid = (
            not result.get("error")
            and result.get("finish") == "stop"
            and verdict in JUDGE_VERDICTS
            and isinstance(reason, str)
            and bool(reason.strip())
        )
        if valid and not result.get("cached"):
            cache.put(key, result)
        return {
            "verdict": verdict if valid else "unclear",
            "reason": reason.strip() if valid else "",
            "error": result.get("error", "")
            or ("" if valid else "invalid cohort adjudication response"),
            "response": result,
        }

    with httpx.Client(limits=httpx.Limits(max_connections=cfg.workers)) as client:
        with ThreadPoolExecutor(cfg.workers) as pool:
            return list(pool.map(one, findings))


def signature_for(f: dict[str, Any], row: Row) -> tuple[dict, dict] | None:
    """The statistical signature and scope that record *f*, or None."""
    cls = f.get("class")
    if row.kind == "prompt" and cls == "shortcut":
        return (
            {"kind": "prompt_contrast", "pattern": row.meta["pattern"]},
            {"scenarios": [row.meta["scenario"]]},
        )
    if row.kind == "case":
        scope = {"fault_types": row.meta["fault_types"]}
        if cls == "label_unidentifiable" and row.meta["confused_with"]:
            sig = {
                "kind": "metric",
                "metric": "label_confusion",
                "threshold": 0.3,
                "min_trials": 3,
                "pattern": re.escape(row.meta["confused_with"]),
            }
        elif cls == "agent_integrity" and row.meta["fault_absent"]:
            sig = {"kind": "validity", "flag": "fault_absent_at_cleanup"}
        elif cls == "agent_integrity":
            # The mutation rate covers the whole submission, not one case.
            sig = {"kind": "metric", "metric": "mutation_rate", "threshold": 0.3}
            return sig, {}
        elif cls == "shortcut":
            sig = {
                "kind": "metric",
                "metric": "case_quick_solve_share",
                "threshold": 0.3,
                "min_trials": 3,
            }
        else:
            sig = {
                "kind": "metric",
                "metric": "case_fail_rate",
                "threshold": 0.8,
                "min_trials": 3,
            }
        return sig, scope
    if row.kind == "healthy":
        return (
            {"kind": "metric", "metric": "healthy_fp_rate", "threshold": 0.5},
            {"scenarios": [row.meta["scenario"]]},
        )
    if row.kind == "tool":
        trunc = row.meta["truncation_rate"] >= row.meta["error_rate"]
        return (
            {
                "kind": "metric",
                "metric": "tool_truncation_rate" if trunc else "tool_error_rate",
                "threshold": 0.2,
                "min_trials": 5,
                # Matched against the tool name followed by its counts.
                "pattern": f"^{re.escape(row.meta['tool'])}(?:\\s|$)",
            },
            {},
        )
    return None


def _same(issue: Issue, sig: dict, scope: dict) -> bool:
    """Same statistic over the same scope, whatever class it was filed under."""
    expected = Scope(**scope).model_dump()
    actual = issue.scope.model_dump()

    def normalize(values):
        return {k: sorted(v) if isinstance(v, list) else v for k, v in values.items()}

    return normalize(actual) == normalize(expected) and any(
        s.model_dump() == Signature(**sig).model_dump() for s in issue.detect
    )


def curate(
    findings: list[dict[str, Any]],
    verdicts: list[dict[str, str]],
    rows: list[Row],
    kb: KnowledgeBase,
    kb_dir: Path,
    current: list[TrialRecord],
    history: list[TrialRecord],
    *,
    source: str,
    date: str,
) -> list[dict[str, Any]]:
    """Write one issue per real finding unless an issue with the same statistic
    and scope exists; the signature must fire on this submission."""
    by_id = {r.id: r for r in rows}
    out = []
    issues = list(kb.issues)
    used = {i.id for i in issues}
    for f, v in zip(findings, verdicts):
        if v["verdict"] != "real":
            continue
        row = by_id[f["row"]]
        made = signature_for(f, row)
        if made is None:
            out.append({"row": row.id, "written": None, "why": "no statistic"})
            continue
        sig, scope = made
        same = next((i for i in issues if _same(i, sig, scope)), None)
        if same is not None:
            out.append({"row": row.id, "written": None, "recorded_by": same.id})
            continue
        prefix = PREFIX.get(f["class"], "ISSUE")
        target = (
            scope.get("fault_types")
            or scope.get("scenarios")
            or [row.meta.get("tool", "")]
        )
        base = f"{prefix}-{re.sub(r'[^A-Z0-9]+', '-', '-'.join(target).upper())}"
        base = base[:60].strip("-")
        issue_id, n = base, 2
        while issue_id in used:
            issue_id, n = f"{base}-{n}", n + 1
        claim = str(f.get("claim") or "")
        observation = row.meta.get("prompt_observation")
        if row.kind == "prompt" and observation:
            claim = (
                f"In {row.env}, the prompt fragment {observation['fragment']!r} "
                f"appears in {observation['fault_occurrences']}/{observation['fault_trials']} "
                f"fault trials and {observation['healthy_occurrences']}/{observation['healthy_trials']} "
                "healthy trials of the same recorded deployment and code version."
            )
        data = {
            "id": issue_id,
            "title": " ".join(claim.split())[:120],
            "class": f["class"],
            "severity": "warn",
            "status": "open",
            "summary": f"{claim} Evidence: {row.text}",
            "guideline": "Recorded from cross-trial statistics; check the row again.",
            "detect": [sig],
            "provenance": {
                "source": source,
                "date": date,
                "refs": row.meta.get("refs", [row.id]),
                "found_by": "agentic",
            },
        }
        if scope:
            data["scope"] = scope
        try:
            issue = Issue(**data)
        except (TypeError, ValueError) as exc:
            out.append({"row": row.id, "written": None, "why": f"invalid: {exc}"})
            continue
        single = kb.model_copy(update={"issues": [issue]})
        if sig["kind"] == "metric" and not run_metrics(single, current, history):
            out.append(
                {"row": row.id, "written": None, "why": "statistic does not fire"}
            )
            continue
        (kb_dir / "issues" / f"{issue_id}.yaml").write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
        )
        issues.append(issue)
        used.add(issue_id)
        out.append({"row": row.id, "written": issue_id})
    return out
