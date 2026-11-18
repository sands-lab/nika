"""Trial-relative event plans with deterministic occurrence identities."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

_TIME = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")


def seconds(value: Any) -> float:
    """Parse a finite duration in seconds or with ms, s, m, and h units."""
    if isinstance(value, bool):
        raise ValueError("Time must be a duration, not a boolean")
    if isinstance(value, str):
        parts = list(_TIME.finditer(value))
        if not parts or "".join(part[0] for part in parts) != value:
            raise ValueError(f"Invalid duration {value!r}; use seconds or e.g. 1m30s")
        value = sum(
            float(part[1]) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[part[2]]
            for part in parts
        )
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Time must be a duration in seconds or use ms, s, m, or h"
        ) from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError("Time must be finite and nonnegative")
    return result


Seconds = Annotated[float, BeforeValidator(seconds)]


class Repeat(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    every: Seconds = Field(gt=0)
    count: int = Field(gt=0, strict=True)


class Burst(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["burst"]
    sources: list[str] = Field(min_length=1)
    destination: str = Field(min_length=1)
    protocol: Literal["udp", "tcp"] = "udp"
    rate: str = "10M"
    packet_size: int = Field(default=1200, ge=1, le=65507)
    seed: int = 42


class ScheduledEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    at: Seconds = 0
    duration: Seconds | None = Field(default=None, gt=0)
    repeat: Repeat | None = None
    problem: str | None = Field(default=None, min_length=1)
    inject: dict[str, Any] = Field(default_factory=dict)
    traffic: Literal["background", "business"] | Burst | None = None
    root_causes: list[dict[str, Any]] | None = None

    @property
    def summary(self) -> str:
        from nika.utils.session_log_summaries import summarize_injection

        if self.problem:
            return summarize_injection(self.problem, resolved_params=self.inject)
        if isinstance(self.traffic, Burst):
            return f"{self.traffic.protocol} burst: {', '.join(self.traffic.sources)} → {self.traffic.destination}"
        return f"{self.traffic} traffic"

    @model_validator(mode="after")
    def validate_payload(self) -> ScheduledEvent:
        if bool(self.problem) == (self.traffic is not None):
            raise ValueError(
                "Each event requires exactly one problem or traffic payload"
            )
        if self.traffic is not None and (self.inject or self.root_causes):
            raise ValueError("Traffic events cannot declare inject or root_causes")
        if isinstance(self.traffic, str) and (self.duration or self.repeat):
            raise ValueError(
                "Persistent workload activation cannot have duration or repeat"
            )
        if isinstance(self.traffic, Burst) and self.duration is None:
            raise ValueError("Burst events require duration")
        if isinstance(self.traffic, Burst):
            from nika.traffic.burst import _rate_bps

            if _rate_bps(self.traffic.rate) <= 0:
                raise ValueError("Burst rate must be positive")
        if self.repeat and (self.duration is None or self.duration > self.repeat.every):
            raise ValueError("Repeated events require duration <= repeat.every")
        return self


@dataclass(frozen=True, order=True)
class ScheduledAction:
    at: float
    priority: int
    event_index: int
    occurrence_index: int

    @property
    def stopping(self) -> bool:
        return self.priority == 0


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    agent_start: Seconds | None = None
    events: list[ScheduledEvent] = Field(default_factory=list)

    @property
    def static(self) -> bool:
        return self.agent_start is None and all(
            e.problem and e.at == 0 and e.duration is None and e.repeat is None
            for e in self.events
        )

    @model_validator(mode="after")
    def validate_workloads(self) -> Schedule:
        agent_start = self.agent_start or 0
        for event in self.events:
            if event.problem and event.duration is not None:
                last_start = event.at + (
                    event.repeat.every * (event.repeat.count - 1) if event.repeat else 0
                )
                if last_start + event.duration <= agent_start:
                    raise ValueError(
                        "A fault window must remain observable after agent_start"
                    )
        workloads = [
            event.traffic for event in self.events if isinstance(event.traffic, str)
        ]
        if len(workloads) != len(set(workloads)):
            raise ValueError("Each persistent workload may be activated only once")
        return self

    def actions(self) -> list[ScheduledAction]:
        actions = []
        for index, event in enumerate(self.events):
            for occurrence in range(event.repeat.count if event.repeat else 1):
                at = event.at + occurrence * (event.repeat.every if event.repeat else 0)
                actions.append(ScheduledAction(at, 1, index, occurrence))
                if event.duration is not None:
                    actions.append(
                        ScheduledAction(at + event.duration, 0, index, occurrence)
                    )
        return sorted(actions)

    def case_value(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True, exclude_defaults=True)

    def activates(self, workload: str) -> bool:
        return any(event.traffic == workload for event in self.events)


def normalize_schedule(
    value: Any, scenario: str, release: str | None = None
) -> Schedule:
    from nika.problems.base import ProblemBase
    from nika.problems.registry import get_problem_class, resolve_problem_name

    plan = Schedule.model_validate(value)
    events = []
    for event in plan.events:
        if event.problem:
            problem = resolve_problem_name(event.problem, scenario, release)
            cls = get_problem_class(problem, scenario, release)
            if cls is None:
                raise ValueError(f"Unknown scheduled failure {event.problem!r}")
            if cls.Params is not None:
                cls.Params.model_validate(event.inject)
            elif event.inject:
                raise ValueError(f"Failure {problem!r} accepts no inject parameters")
            recover = getattr(cls, "recover_fault", None)
            if event.duration is not None and (
                not callable(recover) or recover is ProblemBase.recover_fault
            ):
                raise ValueError(
                    f"Failure {problem!r} does not support scheduled recovery"
                )
            event = event.model_copy(update={"problem": problem})
        events.append(event)
    return plan.model_copy(update={"events": events})


def schedule_for_task(row: dict[str, Any], release: str | None = None) -> Schedule:
    """Supply execution defaults without changing the authored task or its identity."""
    from nika.workflows.benchmark.healthy import is_healthy_case
    from nika.workflows.benchmark.multi_fault import nested_inject_map

    value = row.get("schedule")
    if value is None:
        value = {
            "events": [
                {"problem": name, "inject": params, "at": 0}
                for name, params in nested_inject_map(row).items()
                if not is_healthy_case(name)
            ]
        }
    return normalize_schedule(value, row["scenario"], release)
