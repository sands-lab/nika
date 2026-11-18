import type { CanonicalTraceEvent, ScheduleProgress } from "./api.ts";
import type { OverviewSpan } from "./roles.ts";

export function scheduleRecord(
  event: CanonicalTraceEvent,
): Record<string, unknown> | null {
  const data = event.raw?.data;
  return event.event === "schedule_event" &&
    data &&
    typeof data === "object" &&
    !Array.isArray(data)
    ? (data as Record<string, unknown>)
    : null;
}

export function scheduleSeconds(value: number | null | undefined): string {
  return value == null
    ? "—"
    : `${value < 0 ? "−" : "+"}${Math.abs(value).toFixed(value % 1 ? 2 : 0)}s`;
}

export function isScheduleInternal(event: CanonicalTraceEvent): boolean {
  const record = scheduleRecord(event);
  return record != null && ![
    "applied", "restored", "verified", "recovered", "operation_failed",
  ].includes(String(record.status));
}

export function nikaEventTone(
  event: CanonicalTraceEvent | undefined,
  schedule?: ScheduleProgress | null,
): "failure" | "traffic" | "lifecycle" {
  if (!event) return "lifecycle";
  const row = scheduleRecord(event);
  const occurrence =
    row && schedule?.occurrences.find((o) => o.event_index === row.event_index);
  if (occurrence) return occurrence.kind;
  if (event.event?.startsWith("failure_") || event.event === "network_change")
    return "failure";
  if (
    ["traffic_start", "traffic_stop", "env_workload"].includes(
      event.event ?? "",
    )
  )
    return "traffic";
  return "lifecycle";
}

function conciseLabel(label: string): string {
  return label.replace(/\b[a-z_]+=/g, "");
}

export function scheduleContext(
  schedule: ScheduleProgress,
  stage?: string | null,
) {
  const operation = schedule.occurrences.find(
    (o) => o.state === "starting" || o.state === "stopping",
  );
  const active = schedule.occurrences.filter(
    (o) =>
      o.kind === "failure" && (o.state === "active" || o.state === "stopping"),
  );
  const latest = schedule.occurrences
    .filter((o) => o.actual_start != null)
    .sort(
      (a, b) =>
        (b.actual_stop ?? b.actual_start!) - (a.actual_stop ?? a.actual_start!),
    )[0];
  let currentLabel = "Current";
  let currentValue = "No operations applied yet";
  if (operation) {
    currentLabel = `Current · planned ${scheduleSeconds(operation.state === "stopping" ? operation.planned_stop : operation.planned_start)}`;
    currentValue = conciseLabel(operation.label);
  } else if (active.length) {
    currentLabel =
      active.length === 1
        ? `Active failure · since ${scheduleSeconds(active[0].actual_start)}`
        : `Active failures · ${active.length}`;
    currentValue = active.map((o) => conciseLabel(o.label)).join("; ");
  } else if (latest) {
    currentLabel = `Latest event · ${scheduleSeconds(latest.actual_stop ?? latest.actual_start)}`;
    const action =
      latest.actual_stop != null
        ? latest.kind === "failure"
          ? "recovered"
          : "stopped"
        : latest.kind === "failure"
          ? "injected"
          : "started";
    currentValue = `${conciseLabel(latest.label)} · ${action}`;
  } else if (!schedule.total_operations) {
    currentValue = "No scheduled operations";
  }
  const remaining =
    schedule.next_at == null ? null : schedule.next_at - schedule.elapsed_sec;
  const nextLabel =
    remaining == null
      ? "Next"
      : !schedule.started_at
        ? "Next · timeline pending"
        : remaining > 0
          ? `Next · in ${remaining.toFixed(1)}s`
          : "Next · due now";
  const nextValue = schedule.next_label
    ? `${conciseLabel(schedule.next_label)} · ${scheduleSeconds(schedule.next_at)}`
    : schedule.state === "failed"
      ? "Timeline failed"
      : schedule.state === "cancelled"
        ? "Timeline cancelled"
        : stage?.startsWith("agent")
          ? "Awaiting agent completion"
          : "No remaining events";
  return { currentLabel, currentValue, nextLabel, nextValue };
}

export function schedulePhase(
  schedule: ScheduleProgress,
  stage?: string | null,
): string {
  if (schedule.state === "failed") return "Failed";
  if (schedule.state === "cancelled") return "Cancelled";
  if (stage === "teardown") return "Cleaning up";
  if (stage === "eval") return "Evaluating";
  const operation = schedule.occurrences.find(
    (o) => o.state === "starting" || o.state === "stopping",
  );
  if (operation) {
    if (operation.kind === "failure")
      return operation.state === "stopping"
        ? "Recovering failure"
        : "Injecting failure";
    return operation.state === "stopping"
      ? "Stopping traffic"
      : "Starting traffic";
  }
  const phases: Record<string, string> = {
    deploy: "Preparing environment",
    inject: "Preparing injections",
    "agent: preparing": "Preparing agent",
    "schedule: waiting for agent": "Waiting for agent",
    "agent: starting": "Starting agent",
    "agent: diagnosis": "Agent diagnosing",
    "agent: submission": "Agent submitting",
    agent: "Agent running",
  };
  if (stage) return phases[stage] ?? stage;
  if (!schedule.started_at) return "Preparing";
  if (schedule.state === "complete") return "Complete";
  return schedule.agent_actual_start == null
    ? "Waiting for agent"
    : "Running events";
}

/** Project authored and observed windows onto the existing trace clock. */
export function buildScheduleSpans(
  schedule: ScheduleProgress | null | undefined,
  events: CanonicalTraceEvent[],
): OverviewSpan[] {
  if (!schedule?.started_at) return [];
  const origin = Date.parse(schedule.started_at);
  if (!Number.isFinite(origin)) return [];
  const horizon = Math.max(
    schedule.elapsed_sec,
    schedule.agent_start ?? 0,
    ...schedule.occurrences.map((o) => o.planned_stop ?? o.planned_start + 2),
    1,
  );
  const at = (seconds: number) => origin + seconds * 1000;
  const spans: OverviewSpan[] = schedule.occurrences.map((o) => {
    const records = events.filter((e) => {
      const row = scheduleRecord(e);
      return (
        row?.event_index === o.event_index &&
        row?.occurrence_index === o.occurrence_index
      );
    });
    const anchor =
      records.find((e) => scheduleRecord(e)?.status === "verified") ??
      records.find((e) =>
        ["applied", "restored", "operation_failed"].includes(
          String(scheduleRecord(e)?.status),
        ),
      );
    const plannedStartMs = at(o.planned_start);
    const plannedEndMs = at(o.planned_stop ?? horizon);
    const actualStartMs = o.actual_start == null ? null : at(o.actual_start);
    const actualEndMs =
      actualStartMs == null
        ? null
        : at(o.actual_stop ?? Math.max(o.actual_start!, schedule.elapsed_sec));
    const cycle =
      o.occurrence_count > 1
        ? ` · ${o.occurrence_index + 1}/${o.occurrence_count}`
        : "";
    return {
      id: `schedule-${o.event_index}-${o.occurrence_index}`,
      eventId: anchor?.id ?? "",
      lane: "nika",
      role: "nika",
      label: `${o.label}${cycle} · ${o.state} · planned ${scheduleSeconds(o.planned_start)} → ${o.planned_stop == null ? "teardown" : scheduleSeconds(o.planned_stop)} · applied ${scheduleSeconds(o.actual_start)} · recovered/stopped ${scheduleSeconds(o.actual_stop)}`,
      startMs: Math.min(plannedStartMs, actualStartMs ?? plannedStartMs),
      endMs: Math.max(plannedEndMs, actualEndMs ?? plannedEndMs),
      error: o.state === "failed",
      schedule: {
        caption: conciseLabel(o.label) + cycle,
        kind: o.kind,
        state: o.state,
        plannedStartMs,
        plannedEndMs,
        actualStartMs,
        actualEndMs,
        recovered: o.actual_stop != null,
      },
    };
  });
  const planned = schedule.agent_start ?? schedule.agent_actual_start;
  if (planned != null) {
    const actual = schedule.agent_actual_start;
    const event = events.find(
      (e) => e.event === "agent_start" && e.source === "nika",
    );
    spans.push({
      id: "schedule-agent",
      eventId: event?.id ?? "",
      lane: "model",
      role: "assistant",
      label: `Agent start · planned ${schedule.agent_start == null ? "after initial verification" : scheduleSeconds(planned)} · actual ${scheduleSeconds(actual)}`,
      startMs: at(Math.min(planned, actual ?? planned)),
      endMs: at(Math.max(planned, actual ?? planned)) + 250,
      turnStart: false,
      schedule: {
        caption: "Agent start",
        kind: "agent",
        state: actual == null ? "pending" : "started",
        plannedStartMs: at(planned),
        plannedEndMs: at(planned) + 250,
        actualStartMs: actual == null ? null : at(actual),
        actualEndMs: actual == null ? null : at(actual) + 250,
        recovered: false,
      },
    });
  }
  return spans;
}
