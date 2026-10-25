"""Judge each group of scan findings: real issue, by design, or agent mistake.

The scan reports every observation that might matter, so most groups are
noise: behaviour the environment is designed to show, or the audited agent's
own mistakes. Each group gets one call with the evidence a person would check:
the cited steps with the commands that produced them, the environment brief
from the task prompt, and where the group occurs (fault and healthy trials,
cases, environments). The verdict decides which groups become issues; the
reviewing maintainer still accepts them.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from auditlib.kb import KnowledgeBase
from auditlib.scan import ScanConfig, chat
from auditlib.trace import Trial

VERDICTS = ("real", "by_design", "agent_mistake", "unclear")

SYSTEM = """\
You review one candidate problem that an automated scan reported for NIKA, a \
benchmark for network troubleshooting agents. A NIKA task deploys a network \
environment, injects a fault (or none, in a healthy task), and asks an AI agent \
to diagnose it with tools. The candidate claims a problem with the benchmark, \
its harness and tools, or the submission's run. Decide whether it is one.

Issue classes:
{classes}

Verdicts:
- real: the evidence shows a problem of the benchmark, harness, tools, or run, \
as the class describes. A fault-free environment that shows something looking \
like a fault, which the brief does not state, is real (a lure). A tool that \
fails, truncates, or misreports when called correctly is real.
- by_design: the behaviour is intended and stated in the brief, or is standard \
behaviour of the protocol or software, and would not mislead a careful agent.
- agent_mistake: the observation comes from the audited agent's own action: a \
wrong command or argument, a misreading, or a change it made itself. The \
benchmark and tools behaved as documented.
- unclear: the evidence shown cannot settle it.

Judge from the evidence shown, not from the claim's wording. Reply with JSON \
only: {{"verdict": "<one of real, by_design, agent_mistake, unclear>", \
"reason": "<one or two sentences>"}}"""

_JSON = re.compile(r"\{.*\}", re.S)


def system_prompt(kb: KnowledgeBase) -> str:
    classes = "\n".join(f"- {c.id}: {c.question}" for c in kb.taxonomy)
    return SYSTEM.format(classes=classes)


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n // 2] + "\n[...]\n" + text[-n // 2 :]


def _evidence(trial: Trial, step: int) -> str:
    """The cited step and the agent's call that produced it."""
    steps = trial.steps
    if not 0 <= step < len(steps):
        return f"(step {step} not in trace)"
    s = steps[step]
    call = ""
    if s.kind == "tool_result" and s.call_id:
        prev = next(
            (
                c
                for c in steps[:step]
                if c.kind == "tool_call" and c.call_id == s.call_id
            ),
            None,
        )
        if prev is not None:
            call = f"agent call [{prev.index}] {prev.tool}: {_clip(prev.text, 600)}\n"
    return f"{call}[{s.index}] {s.kind} {s.tool}:\n{_clip(s.text, 2500)}"


def group_prompt(group: dict[str, Any], trials: dict[str, Trial]) -> str:
    refs = group.get("refs") or [e["ref"] for e in group.get("examples", [])]
    parts = [f"Class: {group['class']}", f"Claim: {group['claim']}"]
    for other in group.get("other_claims") or []:
        parts.append(f"Also stated as: {other}")
    parts.append(
        f"Occurs in {group['trials']} trials ({group['fault_trials']} fault, "
        f"{group['healthy_trials']} healthy), {group['cases']} cases, "
        f"environments {', '.join(group['environments'])}."
    )
    brief = ""
    for i, ref in enumerate(refs[:3]):
        trial_id, _, step = ref.rpartition(":")
        trial = trials[trial_id]
        if not brief:
            prompt = next((s for s in trial.steps if s.kind == "prompt"), None)
            brief = _clip(prompt.text, 4000) if prompt else ""
        kind = "healthy task" if trial.is_healthy_case else "fault task"
        parts.append(f"\nEvidence {i + 1}: trial {trial_id} ({kind})")
        try:
            parts.append(_evidence(trial, int(step)))
        except ValueError:
            parts.append("(no step cited)")
    return (
        "Environment brief (task prompt of the first example trial):\n"
        f"{brief}\n\n" + "\n".join(parts)
    )


def _verdict(content: str) -> dict[str, str]:
    match = _JSON.search(content or "")
    try:
        data = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        data = {}
    verdict = str(data.get("verdict") or "unclear")
    return {
        "verdict": verdict if verdict in VERDICTS else "unclear",
        "reason": str(data.get("reason") or ""),
    }


def adjudicate_groups(
    groups: list[dict[str, Any]],
    trials: dict[str, Trial],
    kb: KnowledgeBase,
    cfg: ScanConfig,
) -> list[dict[str, Any]]:
    """One verdict per group, in input order; *trials* maps id to trial."""
    system = system_prompt(kb)

    def run(group: dict[str, Any]) -> dict[str, Any]:
        result = chat(client, cfg, system, group_prompt(group, trials))
        out = _verdict(result.get("content", ""))
        out["error"] = result.get("error", "")
        out["tokens"] = [
            result.get("prompt_tokens") or 0,
            result.get("completion_tokens") or 0,
        ]
        return out

    limits = httpx.Limits(max_connections=cfg.workers)
    with httpx.Client(limits=limits) as client:
        with ThreadPoolExecutor(cfg.workers) as pool:
            return list(pool.map(run, groups))
