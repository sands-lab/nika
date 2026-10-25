"""Per-trial checks: run each knowledge-base signature over one trial.

Every hit cites the trial and the step it came from, so a reviewer (human or
LLM) can open the exact evidence, and a re-run of the same knowledge base
reproduces it.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from auditlib.kb import Issue, KnowledgeBase, Policy, Signature
from auditlib.regrade import regrade_trial
from auditlib.trace import Step, Trial, split_tool_name
from agent.protocols import SUBMISSION
from nika.evaluator.validity import detect_trial_validity

_EXCERPT = 80
# Benchmark terms in agent-visible text. Word starts only, so "default" is no
# hit; "nika_x" and "/nika-x" are.
_TERMS = re.compile(r"(?<![a-z])(?:nika|inject|fault|ground[\s_-]*truth)", re.I)
# Wording of the shared diagnosis task prompt, identical for every agent.
_ALLOWED_PHRASES = re.compile(r"localizing the faulty devices", re.I)
_TERM_EXCERPT = 60


def scan_text(text: str, *, phase: str, authored: str = ""):
    """Yield ``(term, excerpt)`` for each benchmark term in *text*.

    A hit whose word also occurs in *authored* (agent-written text) is
    skipped, and "fault" is expected in the submission phase.
    """
    allowed = [m.span() for m in _ALLOWED_PHRASES.finditer(text)]
    authored = authored.lower()
    for match in _TERMS.finditer(text):
        term = re.sub(r"[\s_-]+", " ", match.group(0).lower())
        if phase == SUBMISSION and term == "fault":
            continue
        if any(start <= match.start() < end for start, end in allowed):
            continue
        word = re.match(r"[\w-]*", text[match.start() :]).group(0).lower()
        if authored and word in authored:
            continue
        start = max(match.start() - _TERM_EXCERPT, 0)
        yield term, text[start : match.end() + _TERM_EXCERPT].replace("\n", " ")


@dataclass
class Hit:
    """One signature match in one trial."""

    issue: str
    trial: str
    step: int | None = None
    line: int | None = None
    excerpt: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "", {})}


def excerpt(text: str, start: int, end: int, width: int = _EXCERPT) -> str:
    lo = max(start - width, 0)
    return text[lo : end + width].replace("\n", " ")


def _gt_types(trial: Trial) -> set[str]:
    return {
        str(rc.get("fault_type"))
        for rc in trial.ground_truth.get("root_causes") or []
        if isinstance(rc, dict)
    }


def in_scope(issue: Issue, trial: Trial) -> bool:
    scope = issue.scope
    if scope.healthy is not None and scope.healthy != trial.is_healthy_case:
        return False
    if scope.fault_types and not (_gt_types(trial) | set(trial.problems)) & set(
        scope.fault_types
    ):
        return False
    if scope.scenarios and trial.scenario not in scope.scenarios:
        return False
    if scope.agent_types and str(trial.run.get("agent_type")) not in scope.agent_types:
        return False
    release = str(trial.run.get("benchmark_version") or "")
    return not (scope.releases and release and release not in scope.releases)


class TrialContext:
    """Per-trial values several signatures share, computed once."""

    def __init__(self, trial: Trial, policy: Policy) -> None:
        self.trial = trial
        self.policy = policy
        self.calls = {s.call_id: s for s in trial.steps if s.kind == "tool_call"}
        self._validity: list[str] | None = None
        self._regrade: list[dict[str, Any]] | None = None

    def call_of(self, step: Step) -> Step | None:
        return self.calls.get(step.call_id) if step.call_id else None

    def tool_of(self, step: Step) -> str:
        if step.kind == "tool_call":
            return step.tool
        call = self.call_of(step)
        return call.tool if call else ""

    @property
    def validity_flags(self) -> list[str]:
        if self._validity is None:
            self._validity = detect_trial_validity(
                self.trial.path, run_meta=self.trial.run
            ).flags
        return self._validity

    @property
    def regrade(self) -> list[dict[str, Any]]:
        if self._regrade is None:
            self._regrade = regrade_trial(self.trial)
        return self._regrade


def _text_hits(issue: Issue, sig: Signature, ctx: TrialContext) -> list[Hit]:
    hits: list[Hit] = []
    assert sig.regex is not None
    for step in ctx.trial.steps:
        if step.kind not in sig.where:
            continue
        if sig.tool_regex and not sig.tool_regex.fullmatch(ctx.tool_of(step)):
            continue
        authored = ""
        if step.kind == "tool_result" and sig.exclude_authored:
            call = ctx.call_of(step)
            authored = call.text.lower() if call else ""
        for match in sig.regex.finditer(step.text):
            if authored and match.group(0).lower() in authored:
                continue
            hits.append(
                Hit(
                    issue.id,
                    ctx.trial.trial_id,
                    step.index,
                    step.line,
                    excerpt(step.text, match.start(), match.end()),
                    {"match": match.group(0)},
                )
            )
            break  # one hit per step is enough evidence
    return hits


def _leak_term_hits(issue: Issue, ctx: TrialContext) -> list[Hit]:
    report = str(ctx.trial.submission.get("diagnosis_report") or "").strip()
    hits: list[Hit] = []
    for step in ctx.trial.steps:
        if step.kind not in ("prompt", "init", "tool_result"):
            continue
        text = step.text
        if step.kind == "prompt" and report:
            text = text.replace(report, "")
        call = ctx.call_of(step)
        authored = call.text if call else ""
        for term, snippet in scan_text(text, phase=step.phase, authored=authored):
            hits.append(
                Hit(
                    issue.id,
                    ctx.trial.trial_id,
                    step.index,
                    step.line,
                    snippet,
                    {"term": term, "source": step.kind},
                )
            )
            break
    return hits


def _fault_token_hits(issue: Issue, ctx: TrialContext) -> list[Hit]:
    """Tool output that names the trial's own fault type."""
    from experiment.audit.agent_view import fault_tokens

    tokens = [t for ft in sorted(_gt_types(ctx.trial)) for t in fault_tokens(ft)]
    if not tokens:
        return []
    regex = re.compile("|".join(tokens), re.IGNORECASE)
    hits: list[Hit] = []
    for step in ctx.trial.steps:
        if step.kind != "tool_result" or step.phase != "diagnosis":
            continue
        call = ctx.call_of(step)
        authored = call.text.lower() if call else ""
        for match in regex.finditer(step.text):
            if match.group(0).lower() in authored:
                continue
            hits.append(
                Hit(
                    issue.id,
                    ctx.trial.trial_id,
                    step.index,
                    step.line,
                    excerpt(step.text, match.start(), match.end()),
                    {"match": match.group(0)},
                )
            )
            break
    return hits


def tool_allowed(name: str, server: str, policy: Policy) -> bool:
    """Whether a tool name (bare, with its MCP server if known) is in policy."""
    full = f"mcp__{server}__{name}" if server else name
    if policy.forbidden_regex.fullmatch(full) or policy.forbidden_regex.fullmatch(name):
        return False
    if server:
        return bool(policy.server_regex.fullmatch(server))
    return True


def _init_tools(step: Step) -> list[str]:
    tools = step.meta.get("tools") or []
    return [str(t) for t in tools] if isinstance(tools, list) else []


def _policy_hits(issue: Issue, sig: Signature, ctx: TrialContext) -> list[Hit]:
    policy = ctx.policy
    narrow = sig.regex
    hits: list[Hit] = []
    if sig.scope == "init":
        for step in ctx.trial.steps:
            if step.kind != "init":
                continue
            bad = []
            for name in _init_tools(step):
                server, tool = split_tool_name(name)
                allowed = tool_allowed(tool, server, policy)
                if not server and allowed:
                    allowed = bool(policy.builtin_regex.fullmatch(tool))
                if not allowed and (narrow is None or narrow.search(name)):
                    bad.append(name)
            for key in ("skills", "plugins"):
                for item in step.meta.get(key) or []:
                    name = f"{key}:{_name(item)}"
                    if narrow is None or narrow.search(name):
                        bad.append(name)
            if bad:
                hits.append(
                    Hit(
                        issue.id,
                        ctx.trial.trial_id,
                        step.index,
                        step.line,
                        ", ".join(sorted(bad)[:12]),
                        {"tools": sorted(bad), "count": len(bad)},
                    )
                )
            break  # the first init lists the diagnosis harness
        return hits
    results = {s.call_id: s for s in ctx.trial.steps if s.kind == "tool_result"}
    for step in ctx.trial.calls():
        result = results.get(step.call_id)
        if result is not None and result.is_error:
            continue  # the harness refused it
        full = f"mcp__{step.server}__{step.tool}" if step.server else step.tool
        if tool_allowed(step.tool, step.server, policy):
            continue
        if narrow is not None and not narrow.search(full):
            continue
        hits.append(
            Hit(
                issue.id,
                ctx.trial.trial_id,
                step.index,
                step.line,
                full + " " + step.text[:120],
            )
        )
    return hits


def _name(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("name") or item.get("id") or item)
    return str(item)


# Search patterns name commands without running them: grep -E 'ip route add'.
_QUOTED_PATTERN_ARGS = re.compile(
    r"\b(?:grep|egrep|fgrep|rg|awk|sed\s+-n)\b(?:\s+-[\w-]+)*\s+(?:'[^']*'|\\?\"[^\"]*\\?\")"
)


def _mutation_hits(issue: Issue, ctx: TrialContext) -> list[Hit]:
    policy = ctx.policy
    hits: list[Hit] = []
    for step in ctx.trial.calls():
        if step.tool not in policy.node_exec_tools:
            continue
        match = policy.mutating_regex.search(_QUOTED_PATTERN_ARGS.sub(" ", step.text))
        if match:
            hits.append(
                Hit(
                    issue.id,
                    ctx.trial.trial_id,
                    step.index,
                    step.line,
                    excerpt(step.text, match.start(), match.end(), 60),
                    {"tool": step.tool},
                )
            )
    return hits


def _regrade_hits(issue: Issue, sig: Signature, ctx: TrialContext) -> list[Hit]:
    hits: list[Hit] = []
    for diff in ctx.regrade:
        cause = diff.get("cause")
        if (sig.explain or "unexplained") != cause:
            continue
        hits.append(
            Hit(
                issue.id,
                ctx.trial.trial_id,
                excerpt=f"{diff['metric']}: submitted {diff['submitted']} regraded {diff['regraded']}",
                detail=diff,
            )
        )
    return hits


def run_trial(kb: KnowledgeBase, trial: Trial) -> list[Hit]:
    """Run every per-trial signature of every in-scope issue on *trial*."""
    ctx = TrialContext(trial, kb.policy)
    hits: list[Hit] = []
    for issue in kb.issues:
        if not in_scope(issue, trial):
            continue
        for sig in issue.detect:
            if sig.kind == "text":
                hits.extend(_text_hits(issue, sig, ctx))
            elif sig.kind == "leak_terms":
                hits.extend(_leak_term_hits(issue, ctx))
            elif sig.kind == "fault_tokens":
                hits.extend(_fault_token_hits(issue, ctx))
            elif sig.kind == "tool_policy":
                hits.extend(_policy_hits(issue, sig, ctx))
            elif sig.kind == "validity" and sig.flag in ctx.validity_flags:
                hits.append(Hit(issue.id, trial.trial_id, excerpt=str(sig.flag)))
            elif sig.kind == "regrade":
                hits.extend(_regrade_hits(issue, sig, ctx))
            elif sig.kind == "mutation":
                hits.extend(_mutation_hits(issue, ctx))
    return hits


def tool_stats(trial: Trial, policy: Policy) -> dict[str, dict[str, int]]:
    """Per tool: calls, error results, and truncated results in one trial."""
    stats: dict[str, dict[str, int]] = {}
    calls = {s.call_id: s for s in trial.steps if s.kind == "tool_call"}
    for step in trial.steps:
        if step.kind == "tool_call":
            row = stats.setdefault(step.tool, {"calls": 0, "errors": 0, "truncated": 0})
            row["calls"] += 1
        elif step.kind == "tool_result":
            call = calls.get(step.call_id)
            if call is None:
                continue
            row = stats.setdefault(call.tool, {"calls": 0, "errors": 0, "truncated": 0})
            if step.is_error or policy.tool_error_regex.search(step.text[:300]):
                row["errors"] += 1
            if policy.truncated_regex.search(step.text[:400]):
                row["truncated"] += 1
    return stats
