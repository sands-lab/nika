"""Timing acceptance checks against real scheduler journals."""

import json
from datetime import datetime
from pathlib import Path

from nika.problems.rca import canonical_root_causes


def assert_schedule_timing(directory, *, agent=False):
    directory = Path(directory)
    plan = json.loads((directory / "schedule.json").read_text())
    rows = [
        json.loads(line)
        for line in (directory / "schedule-events.jsonl").read_text().splitlines()
    ]
    assert not any(row["status"] == "failed" for row in rows)
    expected = {}
    for index, event in enumerate(plan.get("events", [])):
        repeat = event.get("repeat", {})
        for occurrence in range(repeat.get("count", 1)):
            at = event.get("at", 0) + occurrence * repeat.get("every", 0)
            expected[index, occurrence, "start"] = at
            if "duration" in event:
                expected[index, occurrence, "stop"] = at + event["duration"]
    for status, budget in [("dispatched", 2), ("finished", 10)]:
        observed = [row for row in rows if row["status"] == status]
        assert len(observed) == len(expected), (status, observed, expected)
        for row in observed:
            key = row["event_index"], row["occurrence_index"], row["operation"]
            assert row["planned_offset"] == expected[key]
            lateness = row["elapsed"] - expected[key]
            assert 0 <= lateness <= budget, row
    completed = next(row for row in rows if row["status"] == "completed")
    assert completed["elapsed"] >= max(expected.values(), default=0)
    truth = json.loads((directory / "ground_truth.json").read_text())

    def roots(items):
        return {
            json.dumps(root, sort_keys=True) for root in canonical_root_causes(items)
        }

    observed_roots = set()
    for verified in (row for row in rows if row["status"] == "verified"):
        index = verified["event_index"]
        assert roots(verified["root_causes"]) == roots(
            plan["events"][index]["root_causes"]
        )
        applied = next(
            row
            for row in rows
            if row["status"] in {"applied", "restored"}
            and row.get("event_index") == index
            and row.get("occurrence_index") == verified["occurrence_index"]
        )
        assert applied["elapsed"] <= verified["elapsed"]
        observed_roots.update(roots(verified["root_causes"]))
    assert roots(truth["root_causes"]) == observed_roots
    assert truth["is_anomaly"] is bool(observed_roots)
    if agent:
        milestones = [
            json.loads(line)
            for line in (directory / "nika.jsonl").read_text().splitlines()
        ]
        started = next(row for row in rows if row["status"] == "started")
        released = next(row for row in milestones if row["event"] == "agent_start")
        actual = (
            datetime.fromisoformat(released["timestamp"])
            - datetime.fromisoformat(started["timestamp"])
        ).total_seconds()
        target = plan.get("agent_start")
        if target is None:
            target = max(
                (
                    row["elapsed"]
                    for row in rows
                    if row["status"] == "finished" and row["planned_offset"] == 0
                ),
                default=0,
            )
        assert 0 <= actual - target <= 3, (actual, target)
        messages = [
            json.loads(line)
            for line in (directory / "messages.jsonl").read_text().splitlines()
        ]
        frozen = next(row for row in messages if row["event"] == "diagnosis_frozen")
        final_start = max(
            row["timestamp"]
            for row in messages
            if row["event"] == "agent_start"
            and row.get("phase") == "diagnosis"
            and row["timestamp"] < frozen["timestamp"]
        )
        assert completed["timestamp"] <= final_start < frozen["timestamp"]
        embedded = [
            row["data"] for row in milestones if row["event"] == "schedule_event"
        ]
        assert all(row in embedded for row in rows)
