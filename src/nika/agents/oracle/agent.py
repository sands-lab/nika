"""Oracle solver: runs each injected failure's reference diagnosis playbook.

The oracle knows which fault NIKA injected and with which parameters, but it
submits that root cause only after the failure's ``oracle_playbook`` evidence
holds when observed through the session's diagnosis MCP tools. A trial the
oracle does not solve therefore points at a fault the agent toolset cannot
observe, or at a case or scoring defect.

Like ``mock``, it reads host session state, so it runs in the NIKA process,
never in a sandbox.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path
from typing import Any

from langchain_mcp_adapters.client import MultiServerMCPClient

from agent.base import DiagnosisAgent
from agent.protocols import DIAGNOSIS, SUBMISSION
from agent.utils.loggers import MessageLogger, tool_event_payload
from nika.agents.mcp_config import load_session_mcp_config
from nika.agents.oracle.plan import fault_plan, triaged_steps
from nika.problems.support.oracle import Check, OraclePlaybook, OracleStep
from nika.runtime.factory import resolve_backend
from nika.utils.session_artifacts import write_json_atomic
from nika.workflows.benchmark.healthy import HEALTHY_PROBLEM

ORACLE_FILENAME = "oracle.json"
_EXCERPT_CHARS = 1200


def tool_text(output: Any) -> str:
    """Flatten an MCP tool result (text blocks, dict, or str) into text."""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parts = [
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in output
        ]
        return "\n".join(parts)
    if isinstance(output, dict):
        return json.dumps(output, default=str)
    return str(output)


# Results of a call that did not run: tool errors, missing tools, and command
# timeouts. They are not observations, so no expectation holds on them.
_FAILED_CALL = re.compile(
    r'^(tool_error:|tool_unavailable:|\[TIMEOUT\]|\{"error": "(tool_execution|validation)_error")'
)


def _observed(expect: Check, text: str) -> bool:
    """Whether *text* is a real observation that satisfies *expect*."""
    if _FAILED_CALL.match(text.lstrip()):
        return False
    try:
        return expect(text)
    except Exception:  # noqa: BLE001 - an unparseable output does not match
        return False


@dataclass
class StepResult:
    step: int
    purpose: str
    tool: str
    args: dict[str, Any]
    evidence: bool
    expected: str | None
    passed: bool | None
    attempts: int
    excerpt: str


@dataclass
class FaultVerdict:
    fault_type: str
    finding: str
    anomaly: bool
    root_causes: list[dict[str, str]]
    playbook: OraclePlaybook | None = None
    steps: list[StepResult] = field(default_factory=list)
    error: str | None = None

    @property
    def confirmed(self) -> bool:
        if self.error is not None:
            return False
        evidence = [s for s in self.steps if s.evidence]
        return bool(evidence) and all(s.passed for s in evidence)


class OracleAgent(DiagnosisAgent):
    """Reference solver that confirms the injected fault with MCP evidence."""

    def __init__(
        self,
        session_id: str,
        model: str = "oracle",
        max_steps: int = 20,
        stream_output: bool = True,
    ) -> None:
        from nika.utils.session import Session

        self.session_id = session_id
        self.model = model
        self.llm_provider = None
        self.max_steps = max_steps
        self.reasoning_effort = None
        self.max_tokens = None
        self.stream_output = stream_output
        session = Session()
        session.load_running_session(session_id=session_id)
        self.session = session
        self.session_dir = self.trace_dir = session.session_dir
        self.scenario_name = str(getattr(session, "scenario_name", "") or "")
        self.verdicts: list[FaultVerdict] = []

    # -- answer key ---------------------------------------------------------

    def _faults(self) -> list[FaultVerdict]:
        """Playbook and expected root causes for each injected failure."""
        from nika.utils.session_store import SessionStore

        names = list(getattr(self.session, "problem_names", None) or [HEALTHY_PROBLEM])
        records = {
            str(r.get("problem_name")): r
            for r in SessionStore().list_failure_injections(session_id=self.session_id)
            if r.get("status") == "injected"
        }
        scenario_params = dict(getattr(self.session, "scenario_params", None) or {})
        scenario_params.setdefault("backend", resolve_backend(self.session))
        release = getattr(self.session, "benchmark_version", None)
        faults: list[FaultVerdict] = []
        for name in names:
            record = records.get(name)
            if record is None and name != HEALTHY_PROBLEM:
                faults.append(
                    FaultVerdict(name, "", True, [], error="no injection record")
                )
                continue
            snapshot = dict((record or {}).get("injection_params") or {})
            try:
                plan = fault_plan(
                    name,
                    self.scenario_name,
                    scenario_params,
                    snapshot.get("resolved_params") or {},
                    release=release,
                )
            except Exception as exc:  # noqa: BLE001 - record and keep going
                faults.append(
                    FaultVerdict(
                        name, "", True, [], error=f"{type(exc).__name__}: {exc}"
                    )
                )
                continue
            faults.append(
                FaultVerdict(
                    fault_type=name,
                    finding=plan.playbook.finding,
                    anomaly=plan.playbook.anomaly,
                    root_causes=plan.root_causes,
                    playbook=plan.playbook,
                )
            )
        return faults

    # -- diagnosis ----------------------------------------------------------

    async def _run_step(
        self,
        index: int,
        step: OracleStep,
        tools: dict[str, Any],
        outputs: dict[str, str],
        logger: MessageLogger,
        call_ids: count,
    ) -> StepResult:
        try:
            args = dict(step.args(outputs) if callable(step.args) else step.args)
        except Exception as exc:  # noqa: BLE001 - a broken step fails, not the run
            return StepResult(
                index,
                step.purpose,
                step.tool,
                {},
                step.evidence,
                step.expect.description if step.expect else None,
                False,
                0,
                f"step_error: cannot build arguments: {exc!r}",
            )
        text = ""
        passed: bool | None = None
        attempt = 0
        for attempt in range(1, max(1, step.attempts) + 1):
            call_id = f"oracle-{next(call_ids)}"
            logger.log(
                "tool_start",
                tool_event_payload(name=step.tool, input=args, tool_call_id=call_id),
            )
            if step.tool not in tools:
                text = f"tool_unavailable: {step.tool}"
            else:
                try:
                    text = tool_text(await tools[step.tool].ainvoke(args))
                except Exception as exc:  # noqa: BLE001 - a failed call is evidence
                    text = f"tool_error: {exc}"
            logger.log(
                "tool_end",
                tool_event_payload(
                    name=step.tool,
                    input=args,
                    tool_call_id=call_id,
                    output=text,
                    output_type="str",
                ),
            )
            passed = None if step.expect is None else _observed(step.expect, text)
            if passed is not False or attempt >= step.attempts:
                break
            await asyncio.sleep(step.interval_sec)
        if step.save_as:
            outputs[step.save_as] = text
        return StepResult(
            step=index,
            purpose=step.purpose,
            tool=step.tool,
            args=args,
            evidence=step.evidence,
            expected=step.expect.description if step.expect else None,
            passed=passed,
            attempts=attempt,
            excerpt=text[:_EXCERPT_CHARS],
        )

    async def diagnose(self, task_description: str) -> str:
        self.session.load_running_session(session_id=self.session_id)
        logger = MessageLogger(phase=DIAGNOSIS, session_dir=self.trace_dir)
        logger.log(
            "llm_start",
            {
                "messages": {"role": "user", "content": task_description},
                "model": {"name": self.model},
            },
        )
        config = load_session_mcp_config(
            self.session_id,
            self.scenario_name,
            backend=resolve_backend(self.session),
            phase=DIAGNOSIS,
        )
        client = MultiServerMCPClient(connections=config)
        tools = {tool.name: tool for tool in await client.get_tools()}

        call_ids = count(1)
        outputs: dict[str, str] = {}
        index = count(1)
        self.verdicts = self._faults()
        step_lists = triaged_steps([v.playbook for v in self.verdicts])
        for verdict, steps in zip(self.verdicts, step_lists, strict=True):
            for step in steps:
                verdict.steps.append(
                    await self._run_step(
                        next(index), step, tools, outputs, logger, call_ids
                    )
                )

        report = self._report()
        write_json_atomic(
            Path(self.session_dir) / ORACLE_FILENAME,
            {
                "solved": all(v.confirmed for v in self.verdicts),
                "faults": [
                    {
                        "fault_type": v.fault_type,
                        "confirmed": v.confirmed,
                        "finding": v.finding,
                        "error": v.error,
                        "failed_evidence": [
                            s.step for s in v.steps if s.evidence and not s.passed
                        ],
                        "steps": [s.__dict__ for s in v.steps],
                    }
                    for v in self.verdicts
                ],
            },
        )
        logger.log("llm_end", {"text": report})
        return report

    def _report(self) -> str:
        lines = ["# Oracle diagnosis", ""]
        for verdict in self.verdicts:
            status = "CONFIRMED" if verdict.confirmed else "NOT CONFIRMED"
            lines.append(f"## {verdict.fault_type}: {status}")
            if verdict.error:
                lines.append(f"Playbook error: {verdict.error}")
            for s in verdict.steps:
                mark = {True: "PASS", False: "FAIL", None: "INFO"}[s.passed]
                tag = " [evidence]" if s.evidence else ""
                lines.append(f"{s.step}. {mark}{tag} {s.purpose}")
                lines.append(f"   call: {s.tool}({json.dumps(s.args, default=str)})")
                if s.expected:
                    lines.append(f"   expected: {s.expected}")
                excerpt = s.excerpt.strip().replace("\n", "\n   | ")
                lines.append(f"   | {excerpt[:400]}")
            if verdict.confirmed:
                causes = ", ".join(
                    f"{c['fault_type']} at {c['resource_id']}"
                    for c in verdict.root_causes
                )
                lines.append(f"Root cause: {verdict.finding} ({causes or 'none'})")
            lines.append("")
        return "\n".join(lines)

    # -- submission ---------------------------------------------------------

    def _submission(self, context: dict[str, Any]) -> dict[str, Any] | None:
        """Answer backed by confirmed evidence, or ``None`` to abstain."""
        catalog = {str(item["id"]) for item in context.get("resources") or []}
        causes: list[dict[str, str]] = []
        for verdict in self.verdicts:
            if verdict.confirmed:
                causes.extend(
                    c for c in verdict.root_causes if c["resource_id"] in catalog
                )
        if causes:
            return {"is_anomaly": True, "root_causes": causes}
        if self.verdicts and all(v.confirmed and not v.anomaly for v in self.verdicts):
            return {"is_anomaly": False, "root_causes": []}
        # An anomaly needs a root cause; without confirmed evidence, abstain.
        return None

    async def submit(self, diagnosis_report: str, context: dict[str, Any]) -> str:
        logger = MessageLogger(phase=SUBMISSION, session_dir=self.trace_dir)
        logger.log(
            "llm_start",
            {
                "messages": {"role": "user", "content": diagnosis_report},
                "model": {"name": self.model},
            },
        )
        config = load_session_mcp_config(
            self.session_id,
            self.scenario_name,
            backend=resolve_backend(self.session),
            phase=SUBMISSION,
        )
        client = MultiServerMCPClient(connections=config)
        tools = {tool.name: tool for tool in await client.get_tools()}
        submission = self._submission(context)
        if submission is None:
            text = "Abstained: no root cause was confirmed by evidence."
            logger.log("llm_end", {"text": text})
            return text
        logger.log(
            "tool_start",
            tool_event_payload(
                name="submit", input=submission, tool_call_id="oracle-submit"
            ),
        )
        result = await tools["submit"].ainvoke(submission)
        logger.log(
            "tool_end",
            tool_event_payload(
                name="submit",
                input=submission,
                tool_call_id="oracle-submit",
                output=tool_text(result),
                output_type="str",
            ),
        )
        text = f"Submitted: {json.dumps(submission)}"
        logger.log("llm_end", {"text": text})
        return text
