"""Trial outcome vocabulary and failure classification.

Counted outcomes (``success``, ``agent_failed``) are finished slots that
``--resume`` keeps and that enter leaderboard / eval averages. ``agent_failed``
scores as 0.0 — the agent ran its budget or returned without a submission.

``endpoint_failed`` is *not* counted: the LLM/API endpoint (or equivalent
HTTP transport to the model provider) was unreachable, timed out, rate-limited,
or returned a server error. Resume cleans and retries those slots so a dead
vLLM/OpenAI URL does not permanently score as model failure.

This is *not* about the network lab under test (Containerlab/Kathara).

``infra_failed`` is also *not* counted: NIKA or the host failed after ground
truth was written but before the agent demonstrably started (sandbox CLI
missing, MCP gateway bind/URL failure, session store I/O, worker killed during
agent setup). The agent never got a turn, so the slot is retried like
``endpoint_failed``. A failure counts as ``agent_failed`` only when the session
log shows ``agent_start`` *and* the trajectory holds at least one model/tool
event (see :func:`agent_demonstrably_started`).

Case wall-clock kills (``--case-timeout``) default to ``agent_failed``, but when
the session was killed mid-LLM call and most of the budget was spent inside LLM
calls (including the in-flight request), they classify as ``endpoint_failed``
so a hung / retrying model endpoint does not permanently score as capability.
Likewise, an agent CLI that stalls waiting for a model response or exhausts
its API retries is ``endpoint_failed`` (see :func:`trace_shows_endpoint_failure`).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

COUNTED_OUTCOMES = frozenset({"success", "agent_failed"})
ENDPOINT_FAILED = "endpoint_failed"
INFRA_FAILED = "infra_failed"
# Not scored; ``--resume`` and retry passes clean and re-run these slots.
RETRYABLE_OUTCOMES = frozenset({ENDPOINT_FAILED, INFRA_FAILED})
KNOWN_OUTCOMES = frozenset({*COUNTED_OUTCOMES, *RETRYABLE_OUTCOMES})

FailureOutcome = Literal["agent_failed", "endpoint_failed", "infra_failed"]

# Case-timeout → endpoint_failed when LLM wall time dominates agent wall time
# *and* the kill lands mid-LLM call.
_CASE_TIMEOUT_LLM_FRACTION = 0.8
_MESSAGES_FILENAME = "messages.jsonl"
_NIKA_FILENAME = "nika.jsonl"
# ``messages.jsonl`` events that only a running agent produces: LangChain/SDK
# ``llm_*`` / ``tool_*``, Claude CLI ``assistant`` stream events, Codex CLI
# ``item.*`` events, and the frozen diagnosis. Setup markers such as
# ``mcp_config`` / ``prompt`` / ``subprocess_start`` / ``agent_start`` do not count.
_AGENT_ACTIVITY_EVENTS = frozenset(
    {
        "llm_start",
        "llm_end",
        "tool_start",
        "tool_end",
        "assistant",
        "item.started",
        "item.completed",
        "diagnosis_frozen",
    }
)
# Exception class names that almost always mean the model never got a fair turn.
_ENDPOINT_TYPE_NAMES = frozenset(
    {
        "APIConnectionError",
        "APIError",
        "APITimeoutError",
        "APIStatusError",
        "AuthenticationError",
        "ConnectError",
        "ConnectTimeout",
        "ConnectionError",
        "ConnectionResetError",
        "ConnectTimeoutError",
        "HTTPStatusError",
        "InternalServerError",
        "PoolTimeout",
        "ProxyError",
        "RateLimitError",
        "ReadError",
        "ReadTimeout",
        "RemoteProtocolError",
        "ServiceUnavailableError",
        "TimeoutError",
        "WriteError",
        "WriteTimeout",
    }
)

_ENDPOINT_MESSAGE_RE = re.compile(
    r"(?:"
    r"connection refused"
    r"|connection reset"
    r"|connection aborted"
    r"|name or service not known"
    r"|temporary failure in name resolution"
    r"|nodename nor servname"
    r"|max retries exceeded"
    r"|server disconnected"
    r"|cannot connect to host"
    r"|all connection attempts failed"
    r"|failed to establish a new connection"
    r"|network is unreachable"
    r"|remote end closed connection"
    r"|remoteprotocolerror"
    r"|apiconnectionerror"
    r"|apitimeouterror"
    r"|httpx\.(?:connect|read|write|pool|remote)"
    r"|openai\.(?:api(?:connection|timeout)|ratelimit)"
    r"|status(?:\s+code)?\s*[:=]?\s*(?:429|500|502|503|504)\b"
    r"|error code[:\s]+(?:429|500|502|503|504)\b"
    r")",
    re.IGNORECASE,
)

# Statuses agent CLIs retry; exhausting those retries is an endpoint failure.
_RETRYABLE_HTTP_STATUS = frozenset({429, 500, 502, 503, 504, 529})

_MISSING_SUBMISSION_RE = re.compile(
    r"without writing required submission",
    re.IGNORECASE,
)
_CASE_TIMEOUT_RE = re.compile(
    r"case exceeded --case-timeout|agent run exceeded agent\.timeout_sec",
    re.IGNORECASE,
)
_WORKER_EXIT_RE = re.compile(r"trial worker exited with code (-?\d+)")


def _iter_exception_chain(exc: BaseException, *, _seen: set[int] | None = None):
    """Yield ``exc``, nested ``BaseExceptionGroup`` members, and cause/context."""
    seen = _seen if _seen is not None else set()
    oid = id(exc)
    if oid in seen:
        return
    seen.add(oid)
    yield exc
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            yield from _iter_exception_chain(sub, _seen=seen)
    for linked in (exc.__cause__, exc.__context__):
        if linked is not None:
            yield from _iter_exception_chain(linked, _seen=seen)


def _exception_text(exc: BaseException) -> str:
    parts: list[str] = []
    for item in _iter_exception_chain(exc):
        parts.append(type(item).__name__)
        parts.append(str(item))
    return "\n".join(parts)


def is_endpoint_exception(exc: BaseException) -> bool:
    """Return True when the failure looks like LLM/API endpoint / HTTP transport."""
    for item in _iter_exception_chain(exc):
        if type(item).__name__ in _ENDPOINT_TYPE_NAMES:
            return True
        # Built-in connection failures (keep OSError itself out — too broad).
        if isinstance(item, (ConnectionError, TimeoutError, BrokenPipeError)):
            return True
    return bool(_ENDPOINT_MESSAGE_RE.search(_exception_text(exc)))


def _parse_event_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iter_jsonl_events(path: Path):
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def _llm_call_spans(
    root: Path,
) -> tuple[float, list[datetime], datetime | None]:
    """Return completed LLM seconds, unpaired ``llm_start`` times, first event."""
    llm_seconds = 0.0
    open_starts: list[datetime] = []
    first_ts: datetime | None = None

    for event in _iter_jsonl_events(root / _MESSAGES_FILENAME):
        ts = _parse_event_timestamp(event.get("timestamp"))
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if first_ts is None or ts < first_ts:
            first_ts = ts
        name = event.get("event")
        if name == "llm_start":
            open_starts.append(ts)
        elif name == "llm_end" and open_starts:
            start = open_starts.pop(0)
            llm_seconds += max(0.0, (ts - start).total_seconds())
    return llm_seconds, open_starts, first_ts


def trace_ends_in_llm_call(session_dir: str | Path) -> bool:
    """True when ``messages.jsonl`` ends with an unpaired ``llm_start``."""
    return bool(_llm_call_spans(Path(session_dir))[1])


def is_signal_exit_code(code: int | None) -> bool:
    """True for a worker killed by a signal.

    ``multiprocessing`` reports a signal death as ``-signum``; the trial
    worker's SIGTERM handler exits with ``128 + signum``.
    """
    return code is not None and (code < 0 or 128 < code <= 128 + 64)


def _killed_by_signal(exc: BaseException) -> bool:
    for item in _iter_exception_chain(exc):
        if isinstance(item, SystemExit) and isinstance(item.code, int):
            if is_signal_exit_code(item.code):
                return True
    match = _WORKER_EXIT_RE.search(_exception_text(exc))
    return match is not None and is_signal_exit_code(int(match.group(1)))


def case_timeout_endpoint_dominated(
    session_dir: str | Path,
    *,
    until: datetime | None = None,
    llm_fraction_threshold: float = _CASE_TIMEOUT_LLM_FRACTION,
) -> bool:
    """True when a case-timeout looks like a hung / slow model endpoint.

    Requires both:

    1. The trajectory ends inside an LLM call (unpaired ``llm_start`` — killed
       mid-request, including after ``llm_retry`` markers).
    2. LLM wall time (completed spans + the in-flight call through ``until``)
       is at least ``llm_fraction_threshold`` of the agent wall clock.

    Tool-heavy kills after a finished ``llm_end`` stay ``agent_failed`` even if
    earlier LLM calls burned most of the budget.
    """
    root = Path(session_dir)
    end = until or datetime.now(UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)

    llm_seconds, open_starts, first_ts = _llm_call_spans(root)
    # Must still be inside an LLM call when the case watchdog fired.
    if not open_starts:
        return False

    for start in open_starts:
        llm_seconds += max(0.0, (end - start).total_seconds())

    # Prefer session agent_start when present so pre-LLM setup does not inflate
    # the LLM fraction.
    for event in _iter_jsonl_events(root / _NIKA_FILENAME):
        if event.get("event") != "agent_start":
            continue
        agent_start = _parse_event_timestamp(event.get("timestamp"))
        if agent_start is None:
            break
        if agent_start.tzinfo is None:
            agent_start = agent_start.replace(tzinfo=UTC)
        first_ts = agent_start
        break

    if first_ts is None:
        return False
    span = max(0.0, (end - first_ts).total_seconds())
    if span <= 0.0:
        return False
    return (llm_seconds / span) >= float(llm_fraction_threshold)


def trace_shows_endpoint_failure(session_dir: str | Path) -> bool:
    """True when the agent CLI gave up waiting on the model endpoint.

    * Codex ``subprocess_stall`` after failed reconnects, or with no tool call
      in flight: the CLI was waiting for a model response. A stall inside a
      tool call stays a capability failure.
    * Codex ``turn.failed`` with an endpoint error (e.g. HTTP 503 after its
      reconnect attempts).
    * Claude Code exhausting its API retries on 429/5xx with no real model
      reply afterwards.
    """
    open_tools = 0
    claude_gave_up = False
    for event in _iter_jsonl_events(Path(session_dir) / _MESSAGES_FILENAME):
        name = event.get("event")
        if name == "thread.started":
            open_tools = 0
        elif name == "tool_start":
            open_tools += 1
        elif name in ("tool_end", "tool_error"):
            open_tools = max(0, open_tools - 1)
        elif name == "subprocess_stall":
            if event.get("reconnect_failure") or open_tools == 0:
                return True
        elif name == "turn.failed":
            codex = event.get("codex_event")
            error = codex.get("error") if isinstance(codex, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            if isinstance(message, str) and _ENDPOINT_MESSAGE_RE.search(message):
                return True
        elif name in ("system", "assistant"):
            claude = event.get("claude_event")
            if not isinstance(claude, dict):
                continue
            if claude.get("subtype") == "api_retry":
                attempt, limit = claude.get("attempt"), claude.get("max_retries")
                if (
                    isinstance(attempt, int)
                    and isinstance(limit, int)
                    and attempt >= limit
                    and claude.get("error_status") in _RETRYABLE_HTTP_STATUS
                ):
                    claude_gave_up = True
            elif name == "assistant":
                message = claude.get("message")
                # Claude Code reports its give-up as a ``<synthetic>`` message.
                if isinstance(message, dict) and message.get("model") != "<synthetic>":
                    claude_gave_up = False
    return claude_gave_up


def agent_demonstrably_started(session_dir: str | Path) -> bool:
    """True when the agent got a turn: ``agent_start`` plus agent activity.

    Requires the session ``agent_start`` event in ``nika.jsonl`` and at least
    one model/tool event in ``messages.jsonl``.
    """
    root = Path(session_dir)
    if not any(
        event.get("event") == "agent_start"
        for event in _iter_jsonl_events(root / _NIKA_FILENAME)
    ):
        return False
    return any(
        event.get("event") in _AGENT_ACTIVITY_EVENTS
        for event in _iter_jsonl_events(root / _MESSAGES_FILENAME)
    )


def classify_trial_failure(
    exc: BaseException,
    *,
    session_dir: str | Path | None = None,
    until: datetime | None = None,
) -> FailureOutcome:
    """Map a post-inject trial exception to a failure outcome.

    * With ``session_dir`` and no evidence that the agent started →
      ``endpoint_failed`` for endpoint signals, else ``infra_failed``
      (both retryable).
    * The trajectory shows the agent CLI gave up on the model endpoint (Codex
      stall while waiting for a response or endpoint ``turn.failed``, Claude
      Code API retries exhausted)
      → ``endpoint_failed``, even when the run then ends without a submission.
    * Missing submission after the agent returned → ``agent_failed`` (capability).
    * Case or agent (``agent.timeout_sec``) wall-clock budget exceeded →
      ``agent_failed``, unless ``session_dir``
      shows the kill happened mid-LLM and LLM calls consumed most of the budget
      → ``endpoint_failed``.
    * Worker killed by a signal (negative exit code, or ``SystemExit`` 128+N
      from the SIGTERM handler) while the trace ends inside an LLM call →
      ``endpoint_failed``. A kill during a tool call stays ``agent_failed``.
    * LLM/API endpoint / HTTP transport signals → ``endpoint_failed`` (retryable).
    * Everything else → ``agent_failed``.

    ``until`` is the case-kill instant used when attributing an in-flight LLM
    call; defaults to now when omitted.
    """
    if session_dir is not None and not agent_demonstrably_started(session_dir):
        return ENDPOINT_FAILED if is_endpoint_exception(exc) else INFRA_FAILED
    if session_dir is not None and trace_shows_endpoint_failure(session_dir):
        return ENDPOINT_FAILED
    text = _exception_text(exc)
    if _MISSING_SUBMISSION_RE.search(text):
        return "agent_failed"
    if _CASE_TIMEOUT_RE.search(text):
        if session_dir is not None and case_timeout_endpoint_dominated(
            session_dir, until=until
        ):
            return ENDPOINT_FAILED
        return "agent_failed"
    if (
        session_dir is not None
        and _killed_by_signal(exc)
        and trace_ends_in_llm_call(session_dir)
    ):
        # An external watchdog killed the worker while a model request hung.
        return ENDPOINT_FAILED
    if is_endpoint_exception(exc):
        return ENDPOINT_FAILED
    return "agent_failed"
