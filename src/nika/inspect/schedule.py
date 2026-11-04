"""Read scheduler artifacts for the terminal and session viewer."""

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from nika.inspect.models import ScheduleOccurrence, ScheduleProgress
from nika.schedule import Schedule


def _document(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def format_schedule_event(row: dict[str, Any]) -> str | None:
    status = row.get("status")
    if status not in {
        "started",
        "applied",
        "restored",
        "verified",
        "recovered",
        "failed",
        "operation_failed",
        "agent_released",
        "cancelled",
        "completed",
        "closed",
    }:
        return None
    elapsed = row.get("elapsed")
    clock = f"+{elapsed:.2f}s " if isinstance(elapsed, (int, float)) else ""
    label = row.get("summary") or row.get("error") or ""
    if row.get("summary") and row.get("error"):
        label += f" · {row['error']}"
    occurrence = row.get("occurrence_index")
    cycle = f" · occurrence {occurrence + 1}" if isinstance(occurrence, int) else ""
    planned = row.get("planned_offset")
    timing = f" · planned {planned:g}s" if isinstance(planned, (int, float)) else ""
    lateness = row.get("lateness_sec")
    if isinstance(lateness, (int, float)):
        timing += f" · late {lateness:.2f}s"
    return f"schedule {clock}{status} {label}{cycle}{timing}".strip()


def _epoch(value: str) -> float:
    parsed = datetime.fromisoformat(value)
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp()


def read_schedule_progress(directory: Path) -> ScheduleProgress | None:
    from nika.inspect.adapters import iter_jsonl

    path = directory / "schedule.json"
    if not path.is_file():
        return None
    try:
        plan = Schedule.model_validate_json(path.read_text())
    except (OSError, ValidationError):
        return None
    rows = list(iter_jsonl(directory / "schedule-events.jsonl"))
    status = _document(directory / "schedule-status.json")
    started = next((row for row in rows if row.get("status") == "started"), None)
    closed = any(row.get("status") == "closed" for row in rows)
    occurrences = {}
    actions = plan.actions()
    for action in actions:
        if action.stopping:
            continue
        event = plan.events[action.event_index]
        occurrences[action.event_index, action.occurrence_index] = ScheduleOccurrence(
            event_index=action.event_index,
            occurrence_index=action.occurrence_index,
            occurrence_count=event.repeat.count if event.repeat else 1,
            label=event.summary,
            kind="failure" if event.problem else "traffic",
            planned_start=action.at,
            planned_stop=action.at + event.duration
            if event.duration is not None
            else None,
        )
    dispatched, finished = set(), set()
    for row in rows:
        key = row.get("event_index"), row.get("occurrence_index")
        occurrence = occurrences.get(key)
        if occurrence is None:
            continue
        operation = (*key, row.get("operation"))
        phase = row.get("status")
        if phase == "dispatched":
            dispatched.add(operation)
        elif phase == "finished":
            finished.add(operation)
        elif phase == "executing":
            occurrence.state = (
                "stopping" if row.get("operation") == "stop" else "starting"
            )
        elif phase in {"applied", "restored"}:
            occurrence.actual_start = row.get("elapsed")
            occurrence.state = "active"
        elif phase == "verified":
            occurrence.verified_at = row.get("elapsed")
            occurrence.state = "active"
        elif phase == "recovered":
            occurrence.actual_stop = row.get("elapsed")
            occurrence.state = "recovered"
        elif phase == "cancelled" and occurrence.state == "pending":
            occurrence.state = "cancelled"
        elif phase == "operation_failed":
            occurrence.state = "failed"
    if closed:
        for occurrence in occurrences.values():
            if occurrence.state in {"active", "starting", "stopping"}:
                occurrence.state = "ended"
    error = status.get("error") or next(
        (row.get("error") for row in rows if row.get("status") == "failed"), None
    )
    state = (
        "failed"
        if error
        else "complete"
        if status.get("completed")
        else "cancelled"
        if status.get("cancelled")
        else "running"
        if started
        else "pending"
    )
    elapsed = float(rows[-1].get("elapsed") or 0) if rows else 0
    actual_agent = None
    started_at = None
    if started:
        run = _document(directory / "run.json")
        try:
            origin = _epoch(started["timestamp"]) + float(
                run.get("remote_clock_offset_sec") or 0
            )
            started_at = datetime.fromtimestamp(origin, UTC).isoformat()
            if not closed and state in {"running", "complete"}:
                elapsed = max(elapsed, datetime.now(UTC).timestamp() - origin)
            agent = next(
                (
                    row
                    for row in iter_jsonl(directory / "nika.jsonl")
                    if row.get("event") == "agent_start"
                ),
                None,
            )
            if agent:
                actual_agent = max(0, _epoch(agent["timestamp"]) - origin)
        except (KeyError, TypeError, ValueError):
            pass
    next_action = (
        next(
            (
                action
                for action in actions
                if (
                    action.event_index,
                    action.occurrence_index,
                    "stop" if action.stopping else "start",
                )
                not in dispatched
            ),
            None,
        )
        if state in {"pending", "running"}
        else None
    )
    next_label = None
    if next_action:
        event = plan.events[next_action.event_index]
        operation = (
            "recover"
            if next_action.stopping and event.problem
            else "stop"
            if next_action.stopping
            else "inject"
            if event.problem
            else "start"
        )
        next_label = f"{operation} {event.summary}"
    return ScheduleProgress(
        state=state,
        started_at=started_at,
        elapsed_sec=elapsed,
        completed_operations=len(finished),
        total_operations=len(actions),
        active_occurrences=sum(
            row.state in {"active", "stopping"} for row in occurrences.values()
        ),
        agent_start=plan.agent_start,
        agent_actual_start=actual_agent,
        next_label=next_label,
        next_at=next_action.at if next_action else None,
        error=str(error) if error else None,
        occurrences=list(occurrences.values()),
    )
