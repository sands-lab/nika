"""Unstructured submission: NIKA maps the frozen report to the scored answer.

``agent.submission.mode`` selects who answers:

* ``structured`` (default): the agent's own model, in the agent's sandbox
  (agent package, not this module).
* ``unstructured``: a :class:`ReportMapper` with one or more configured
  models, for every agent type. This step is part of scoring, so its prompt,
  context formatting, model client, and traces live here.

Each model makes ``samples`` independent calls that return a JSON answer,
checked against the frozen catalogs (one corrective retry per sample), and
settles on its majority answer (:func:`vote`). With several models, each
model's answer is one vote; the answer with the most models wins, ties go to
the highest-weight supporter and then to configuration order, and the winner
needs ``min_agreement`` models (:func:`agree`). NIKA submits the chosen answer
once through the task MCP ``submit`` tool, which validates and records it as
usual.

Each attempt is traced as ``submission_sample`` (raw text, parsed answer or
error), each model's vote as ``submission_vote`` (per-sample answers, counts,
chosen answer), and with several models the vote across them as
``submission_agreement``.

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


def agree(
    model_answers: list[dict[str, Any] | None],
    weights: list[float],
    *,
    min_agreement: int = 1,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Vote across models: ``(chosen answer or None, tally)``.

    Each model's answer is one vote; ``None`` abstains. The tally lists each
    distinct answer with its ``count`` and 1-based ``models``, winner first:
    most models, then the highest supporter weight, then the earliest
    supporter. The winner needs at least *min_agreement* models.
    """
    supporters: dict[str, list[int]] = {}
    for index, answer in enumerate(model_answers):
        if answer is not None:
            key = json.dumps(answer, sort_keys=True)
            supporters.setdefault(key, []).append(index)
    ranked = sorted(
        supporters.items(),
        key=lambda item: (
            -len(item[1]),
            -max(weights[i] for i in item[1]),
            item[1][0],
        ),
    )
    tally = [
        {
            "answer": json.loads(key),
            "count": len(models),
            "models": [i + 1 for i in models],
        }
        for key, models in ranked
    ]
    if not tally or tally[0]["count"] < min_agreement:
        return None, tally
    return tally[0]["answer"], tally


@dataclass
class MapperModel:
    """One mapping model and its sampling settings."""

    llm: Runnable
    provider: str
    model: str
    samples: int = 1
    temperature: float | None = None
    weight: float = 1.0

    def trace_fields(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "samples": self.samples,
            "temperature": self.temperature,
        }


@dataclass
class ReportMapper:
    """Unstructured submission: map the frozen report with a configured model.

    Build it before entering the agent's provider environment
    (:meth:`from_settings`): the chat client keeps the submission provider's
    credentials, which the agent's environment may clear. :meth:`run` is the
    whole submission phase; :meth:`submit` maps and records the answer.
    """

    models: list[MapperModel]
    min_agreement: int = 1

    @classmethod
    def from_settings(
        cls, settings: AgentSubmissionSettings, *, max_tokens: int | None = None
    ) -> ReportMapper | None:
        """Return the mapper for ``mode: unstructured``, else ``None``."""
        if settings.mode != "unstructured":
            return None
        from nika.evaluator.model_client import load_model

        models = []
        for mapper in settings.mapper_settings():
            llm: Runnable = load_model(
                mapper.provider,
                mapper.model,
                max_tokens=max_tokens,
                base_url=mapper.base_url,
            )
            if mapper.temperature is not None:
                llm = llm.bind(temperature=mapper.temperature)
            models.append(
                MapperModel(
                    llm=llm,
                    provider=mapper.provider,
                    model=mapper.model,
                    samples=mapper.samples,
                    temperature=mapper.temperature,
                    weight=mapper.weight,
                )
            )
        return cls(models=models, min_agreement=settings.min_agreement)

    def trace_fields(self) -> dict[str, Any]:
        if len(self.models) == 1:
            return {"submission_mode": "unstructured", **self.models[0].trace_fields()}
        return {
            "submission_mode": "unstructured",
            "mappers": [{**m.trace_fields(), "weight": m.weight} for m in self.models],
            "min_agreement": self.min_agreement,
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
        several = len(self.models) > 1
        model_answers = []
        for index, mapper in enumerate(self.models, start=1):
            # With several models, rows name the model they belong to.
            label = (
                {"mapper": index, "provider": mapper.provider, "model": mapper.model}
                if several
                else {}
            )
            prefix = f"{mapper.model}: " if several else ""
            per_sample = [
                await self._sample(
                    mapper,
                    diagnosis_report,
                    context,
                    sample=sample,
                    catalog_ids=catalog_ids,
                    fault_types=fault_types,
                    logger=logger,
                    label=label,
                    prefix=prefix,
                )
                for sample in range(1, mapper.samples + 1)
            ]
            answers = [answer for answer in per_sample if answer is not None]
            votes = vote(answers)
            model_answers.append(votes[0]["answer"] if votes else None)
            logger.log(
                "submission_vote",
                {
                    **label,
                    "answers": per_sample,
                    "votes": votes,
                    "chosen": model_answers[-1],
                    "message": f"{prefix}{len(answers)}/{mapper.samples} "
                    "valid sample(s)",
                },
            )
        if not several:
            chosen = model_answers[0]
            if chosen is None:
                samples = self.models[0].samples
                return f"{ERROR_PREFIX} no valid answer in {samples} sample(s)"
        else:
            chosen, tally = agree(
                model_answers,
                [m.weight for m in self.models],
                min_agreement=self.min_agreement,
            )
            agreeing = tally[0]["count"] if tally else 0
            logger.log(
                "submission_agreement",
                {
                    "answers": model_answers,
                    "votes": tally,
                    "min_agreement": self.min_agreement,
                    "chosen": chosen,
                    "message": f"{agreeing}/{len(self.models)} model(s) agree "
                    f"(min_agreement {self.min_agreement})",
                },
            )
            if chosen is None:
                return (
                    f"{ERROR_PREFIX} no answer reached min_agreement "
                    f"{self.min_agreement}: at most {agreeing} of "
                    f"{len(self.models)} model(s) agree"
                )
        return await _record(
            chosen, session_id=session_id, scenario_name=scenario_name, logger=logger
        )

    async def _sample(
        self,
        mapper: MapperModel,
        diagnosis_report: str,
        context: dict[str, Any],
        *,
        sample: int,
        catalog_ids: set[str],
        fault_types: set[str],
        logger: TraceLogger,
        label: dict[str, Any],
        prefix: str,
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
                reply = await mapper.llm.ainvoke(
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
                    **label,
                    "sample": sample,
                    "attempt": attempt,
                    "text": text,
                    "answer": answer,
                    "error": error or None,
                    "message": f"{prefix}sample {sample} attempt {attempt}: "
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
