"""Unstructured submission: NIKA maps the frozen report to the scored answer.

``agent.submission.mode`` selects who answers:

* ``structured`` (default): the agent's own model, in the agent's sandbox
  (agent package, not this module).
* ``unstructured``: a :class:`ReportMapper` with its own configured model, for
  every agent type. This step is part of scoring, so its prompt, context
  formatting, model client, and traces live here. Each of ``samples`` independent model calls returns a JSON
  answer, checked against the frozen catalogs (one corrective retry per
  sample). NIKA submits the majority answer once through the task MCP
  ``submit`` tool, which validates and records it as usual. Each attempt is
  traced as ``submission_sample`` (raw text, parsed answer or error) and the
  vote as ``submission_vote`` (per-sample answers, counts, chosen answer).

Neither mode reaches the network: submission sees only the frozen report, the
fault ontology, and the resource catalog.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import dataclass
from textwrap import dedent
from typing import TYPE_CHECKING, Any

from agent.protocols import SUBMISSION
from agent.runtime.contract import ERROR_PREFIX
from nika.evaluator.traces import (
    ModelCallTracer,
    TraceLogger,
    message_text,
    tool_event_payload,
)

if TYPE_CHECKING:
    from langchain_core.runnables import Runnable

    from nika.run_config.schema import AgentSubmissionSettings

# Model calls per sample: the first answer plus one corrective retry.
_ATTEMPTS_PER_SAMPLE = 2

MAP_REPORT_PROMPT = dedent("""\
    You are an expert network engineer.
    Map a frozen network diagnosis report to the benchmark answer format.
    You are given the report, a fault ontology, and a canonical resource
    inventory. Do not investigate again or revise the report; answer only from
    what it states. Reply with one JSON object and nothing else:
    {"is_anomaly": true, "root_causes": [{"resource_id": "<inventory id>", "fault_type": "<ontology id>"}]}
    Each fault ontology entry has an ``id``, a short ``description`` of what
    that id means, and an ``owner_kind``. Each fault_type must be an exact
    ontology ``id`` (never an owner_kind, description, or natural-language
    alias), and each resource_id must be an exact inventory id. Do not invent
    ids. If the report finds no fault, reply
    {"is_anomaly": false, "root_causes": []}.\
""").strip()


def map_report_user_prompt(diagnosis_report: str, context: dict[str, Any]) -> str:
    """User turn of a mapping call: the report and the frozen submission context."""
    frozen = json.dumps(
        {
            "fault_ontology": context["fault_ontology"],
            "resources": context["resources"],
        },
        sort_keys=True,
    )
    return (
        f"Diagnosis report:\n{diagnosis_report}\n"
        f"Frozen submission context (do not query the network):\n{frozen}"
    )


def parse_answer(
    text: str, *, catalog_ids: set[str], fault_types: set[str]
) -> tuple[dict[str, Any] | None, str]:
    """Return ``(answer, "")`` for a valid JSON answer, else ``(None, error)``."""
    from nika.mcp.servers.common.task_server import validate_root_cause_choices

    raw = _last_answer_object(text)
    if raw is None:
        return None, "no JSON object with is_anomaly found."
    if type(raw.get("is_anomaly")) is not bool:
        return None, "is_anomaly must be a boolean."
    causes = raw.get("root_causes") or []
    if not isinstance(causes, list):
        return None, "root_causes must be a list."
    if raw["is_anomaly"] != bool(causes):
        return None, "root_causes must be non-empty exactly when is_anomaly is true."
    parsed, errors = validate_root_cause_choices(
        causes, catalog_ids=catalog_ids, fault_types=fault_types
    )
    if errors:
        return None, " ".join(errors)
    pairs = sorted({(c["resource_id"], c["fault_type"]) for c in parsed})
    return {
        "is_anomaly": raw["is_anomaly"],
        "root_causes": [{"resource_id": r, "fault_type": f} for r, f in pairs],
    }, ""


def _last_answer_object(text: str) -> dict[str, Any] | None:
    """Last JSON object in *text* that has an ``is_anomaly`` key."""
    decoder = json.JSONDecoder()
    found = None
    index = text.find("{")
    while index >= 0:
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError:
            index = text.find("{", index + 1)
            continue
        if isinstance(value, dict) and "is_anomaly" in value:
            found = value
        index = text.find("{", end)
    return found


def vote(answers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Distinct answers with their counts, the majority first.

    Ties keep sample order, so the earliest of the tied answers wins.
    """
    counts = Counter(json.dumps(answer, sort_keys=True) for answer in answers)
    return [
        {"answer": json.loads(key), "count": count}
        for key, count in counts.most_common()
    ]


@dataclass
class ReportMapper:
    """Unstructured submission: map the frozen report with a configured model.

    Build it before entering the agent's provider environment
    (:meth:`from_settings`): the chat client keeps the submission provider's
    credentials, which the agent's environment may clear. :meth:`run` is the
    whole submission phase; :meth:`submit` maps and records the answer.
    """

    llm: Runnable
    provider: str
    model: str
    samples: int = 1
    temperature: float | None = None

    @classmethod
    def from_settings(
        cls, settings: AgentSubmissionSettings, *, max_tokens: int | None = None
    ) -> ReportMapper | None:
        """Return the mapper for ``mode: unstructured``, else ``None``."""
        if settings.mode != "unstructured":
            return None
        from nika.evaluator.model_client import load_model

        provider = str(settings.provider)
        model = str(settings.model)
        llm: Runnable = load_model(
            provider, model, max_tokens=max_tokens, base_url=settings.base_url
        )
        if settings.temperature is not None:
            llm = llm.bind(temperature=settings.temperature)
        return cls(
            llm=llm,
            provider=provider,
            model=model,
            samples=settings.samples,
            temperature=settings.temperature,
        )

    def trace_fields(self) -> dict[str, Any]:
        return {
            "submission_mode": "unstructured",
            "provider": self.provider,
            "model": self.model,
            "samples": self.samples,
            "temperature": self.temperature,
        }

    async def run(
        self,
        diagnosis_report: str,
        context: dict[str, Any],
        *,
        session_id: str,
        scenario_name: str,
        trace_dir: str,
        stream_output: bool,
    ) -> str:
        """Run the submission phase between its ``agent_start`` and ``agent_done`` / ``agent_error`` rows.

        A result that starts with ``ERROR:`` is logged as ``agent_error`` and
        returned; the run goes on and is scored as it stands.
        """
        logger = TraceLogger(SUBMISSION, trace_dir)
        _print_phase(stream_output, "recording structured result")
        logger.log_agent_start(**self.trace_fields())
        try:
            result = await self.submit(
                diagnosis_report,
                context,
                session_id=session_id,
                scenario_name=scenario_name,
                trace_dir=trace_dir,
                stream_output=stream_output,
            )
        except BaseException as exc:
            logger.log_agent_error(exc)
            _print_phase(stream_output, f"failed ({str(exc)[:120]})")
            raise
        if result.startswith(ERROR_PREFIX):
            logger.log_agent_error(result)
            _print_phase(stream_output, f"failed ({result[:120]})")
        else:
            logger.log_agent_done(result_length=len(result))
            _print_phase(stream_output, "completed")
        return result

    async def submit(
        self,
        diagnosis_report: str,
        context: dict[str, Any],
        *,
        session_id: str,
        scenario_name: str,
        trace_dir: str,
        stream_output: bool = False,
    ) -> str:
        catalog_ids = {
            str(item["id"])
            for item in context["resources"]
            if isinstance(item, dict) and item.get("id")
        }
        fault_types = {
            str(item.get("id")) if isinstance(item, dict) else str(item)
            for item in context["fault_ontology"]
        }
        logger = TraceLogger(SUBMISSION, trace_dir, echo=stream_output)
        per_sample = [
            await self._sample(
                diagnosis_report,
                context,
                sample=index,
                catalog_ids=catalog_ids,
                fault_types=fault_types,
                logger=logger,
            )
            for index in range(1, self.samples + 1)
        ]
        answers = [answer for answer in per_sample if answer is not None]
        votes = vote(answers)
        chosen = votes[0]["answer"] if votes else None
        logger.log(
            "submission_vote",
            {
                "answers": per_sample,
                "votes": votes,
                "chosen": chosen,
                "message": f"{len(answers)}/{self.samples} valid sample(s)",
            },
        )
        if chosen is None:
            return f"{ERROR_PREFIX} no valid answer in {self.samples} sample(s)"
        return await _record(
            chosen, session_id=session_id, scenario_name=scenario_name, logger=logger
        )

    async def _sample(
        self,
        diagnosis_report: str,
        context: dict[str, Any],
        *,
        sample: int,
        catalog_ids: set[str],
        fault_types: set[str],
        logger: TraceLogger,
    ) -> dict[str, Any] | None:
        """One mapping of the report; each attempt is logged as ``submission_sample``."""
        from langchain_core.messages import HumanMessage, SystemMessage

        messages: list[Any] = [
            SystemMessage(content=MAP_REPORT_PROMPT),
            HumanMessage(content=map_report_user_prompt(diagnosis_report, context)),
        ]
        callbacks = [ModelCallTracer(logger)]
        for attempt in range(1, _ATTEMPTS_PER_SAMPLE + 1):
            reply, text, answer = None, None, None
            try:
                reply = await self.llm.ainvoke(
                    messages, config={"callbacks": callbacks}
                )
            except Exception as exc:  # noqa: BLE001 - other samples can still vote
                error = f"model call failed: {type(exc).__name__}: {exc}"
            else:
                text = message_text(reply)
                answer, error = parse_answer(
                    text, catalog_ids=catalog_ids, fault_types=fault_types
                )
            logger.log(
                "submission_sample",
                {
                    "sample": sample,
                    "attempt": attempt,
                    "text": text,
                    "answer": answer,
                    "error": error or None,
                    "message": f"sample {sample} attempt {attempt}: "
                    + ("valid" if answer is not None else f"invalid ({error})"),
                },
            )
            if reply is None or answer is not None:
                return answer
            messages += [
                reply,
                HumanMessage(
                    content=f"Invalid answer: {error} Reply with the corrected "
                    "JSON object only."
                ),
            ]
        return None


def _print_phase(stream_output: bool, message: str) -> None:
    if not stream_output:
        return
    banner = f" [{SUBMISSION.upper()}] {message} "
    width = max(60, len(banner) + 4)
    print(f"\n{'=' * width}", file=sys.stderr, flush=True)
    print(banner.center(width), file=sys.stderr, flush=True)
    print(f"{'=' * width}\n", file=sys.stderr, flush=True)


async def _record(
    answer: dict[str, Any], *, session_id: str, scenario_name: str, logger: TraceLogger
) -> str:
    """Record *answer* through the task MCP ``submit`` tool."""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    from nika.agents.mcp_config import client_mcp_config, load_session_mcp_config

    config = client_mcp_config(
        load_session_mcp_config(session_id, scenario_name, phase=SUBMISSION)
    )
    tools = {t.name: t for t in await MultiServerMCPClient(config).get_tools()}
    call = {"name": "submit", "input": answer, "tool_call_id": "nika-submit"}
    logger.log("tool_start", tool_event_payload(**call))
    output = str(await tools["submit"].ainvoke(answer))
    logger.log("tool_end", tool_event_payload(**call, output=output))
    if "Submission success" not in output:
        return f"{ERROR_PREFIX} submit rejected the mapped answer: {output}"
    return f"Submitted: {json.dumps(answer, sort_keys=True)}"
