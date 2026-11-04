import assert from "node:assert/strict";
import test from "node:test";
import { buildScheduleSpans } from "../src/scheduleView.ts";
import { projectOverviewDomain } from "../src/roles.ts";

const origin = "2026-11-04T10:00:00Z";
const plan = {
  state: "running",
  started_at: origin,
  elapsed_sec: 20,
  agent_start: 10,
  agent_actual_start: 10.1,
  occurrences: [
    {
      kind: "failure",
      event_index: 0,
      occurrence_index: 0,
      occurrence_count: 2,
      label: "link_down: host_name=r1, intf_name=eth0",
      state: "recovered",
      planned_start: 2,
      planned_stop: 5,
      actual_start: 2.4,
      actual_stop: 5.3,
    },
    {
      kind: "failure",
      event_index: 0,
      occurrence_index: 1,
      occurrence_count: 2,
      label: "link_down: host_name=r1, intf_name=eth0",
      state: "pending",
      planned_start: 32,
      planned_stop: 35,
      actual_start: null,
      actual_stop: null,
    },
    {
      kind: "traffic",
      event_index: 1,
      occurrence_index: 0,
      occurrence_count: 1,
      label: "background traffic",
      state: "active",
      planned_start: 3,
      planned_stop: null,
      actual_start: 3.5,
      actual_stop: null,
    },
  ],
};
const event = (id, index, occurrence, status) => ({
  id,
  event: "schedule_event",
  source: "nika",
  raw: { data: { event_index: index, occurrence_index: occurrence, status } },
});

test("scheduled windows share the trace clock, preserve skew, and link to actual logs", () => {
  const events = [
    event("inject", 0, 0, "applied"),
    event("verify", 0, 0, "verified"),
  ];
  const spans = buildScheduleSpans(plan, events);
  const first = spans[0];
  assert.equal(first.lane, "nika");
  assert.equal(first.schedule.kind, "failure");
  assert.equal(first.eventId, "verify");
  assert.equal(first.startMs, Date.parse(origin) + 2000);
  assert.equal(first.endMs, Date.parse(origin) + 5300);
  assert.equal(
    first.schedule.actualStartMs - first.schedule.plannedStartMs,
    400,
  );
  assert.match(first.label, /r1.*1\/2.*recovered/);
  assert.equal(spans[1].eventId, "");
  assert.equal(spans[1].schedule.actualStartMs, null);
  assert.equal(spans[2].lane, "nika");
  assert.equal(spans[2].schedule.kind, "traffic");
  assert.equal(spans[2].schedule.actualEndMs, Date.parse(origin) + 20000);
  assert.equal(spans[3].lane, "model");
  for (const mode of ["equal", "duration", "actual"]) {
    const projected = projectOverviewDomain(spans, mode);
    assert.equal(projected.items.length, 4);
    assert.ok(projected.items.every((it) => it.x1 >= it.x0));
  }
});

test("closed persistent faults keep their observed window without claiming recovery", () => {
  const fault = {
    ...plan.occurrences[0],
    planned_stop: null,
    actual_stop: null,
    state: "ended",
  };
  const spans = buildScheduleSpans(
    { ...plan, state: "complete", occurrences: [fault] },
    [],
  );
  assert.equal(spans[0].schedule.recovered, false);
  assert.equal(spans[0].schedule.actualEndMs, Date.parse(origin) + 20000);
  assert.match(spans[0].label, /teardown.*recovered\/stopped —/);
});

test("pending plans do not invent a timeline origin", () => {
  assert.deepEqual(buildScheduleSpans({ ...plan, started_at: null }, []), []);
  assert.deepEqual(buildScheduleSpans(null, []), []);
});

test("scheduler phase follows the running agent after finite events complete", async () => {
  const { schedulePhase } = await import("../src/scheduleView.ts");
  assert.equal(
    schedulePhase({ ...plan, state: "complete" }, "agent: diagnosis"),
    "Agent diagnosing",
  );
  assert.equal(
    schedulePhase({ ...plan, state: "complete" }, "teardown"),
    "Cleaning up",
  );
  assert.equal(schedulePhase({ ...plan, state: "complete" }), "Complete");
  assert.equal(
    schedulePhase(
      { ...plan, agent_actual_start: null },
      "schedule: waiting for agent",
    ),
    "Waiting for agent",
  );
  assert.equal(
    schedulePhase(
      { ...plan, occurrences: [{ ...plan.occurrences[0], state: "stopping" }] },
      "agent: diagnosis",
    ),
    "Recovering failure",
  );
});

test("NIKA subtype shades follow declared occurrence kinds", async () => {
  const { nikaEventTone } = await import("../src/scheduleView.ts");
  assert.equal(
    nikaEventTone(event("fault", 0, 1, "verified"), plan),
    "failure",
  );
  assert.equal(
    nikaEventTone(event("traffic", 1, 0, "applied"), plan),
    "traffic",
  );
  assert.equal(nikaEventTone({ event: "env_verify", raw: {} }), "lifecycle");
});

test("scheduler context names targets, shows deadlines, and retains the latest result", async () => {
  const { scheduleContext } = await import("../src/scheduleView.ts");
  const pending = scheduleContext({
    ...plan,
    next_at: 32,
    next_label: "inject link_down: host_name=r1, intf_name=eth0",
  });
  assert.match(pending.currentLabel, /Latest event.*5.30s/);
  assert.match(pending.currentValue, /link_down: r1, eth0.*recovered/);
  assert.equal(pending.nextLabel, "Next · in 12.0s");
  assert.match(pending.nextValue, /inject link_down: r1, eth0.*32s/);
  const active = scheduleContext(
    {
      ...plan,
      occurrences: [
        {
          ...plan.occurrences[0],
          actual_start: 12,
          actual_stop: null,
          state: "active",
        },
      ],
    },
    "agent: diagnosis",
  );
  assert.match(active.currentLabel, /Active failure.*12s/);
  assert.match(active.currentValue, /r1/);
  assert.equal(active.nextValue, "Awaiting agent completion");
});

test("timeline retains scheduled actions and hides scheduler bookkeeping", async () => {
  const { isScheduleInternal } = await import("../src/scheduleView.ts");
  for (const status of ["started", "dispatched", "executing", "finished", "completed", "closed", "cancelled", "failed", "agent_released"]) {
    assert.equal(isScheduleInternal(event(status, 0, 0, status)), true, status);
  }
  for (const status of ["applied", "restored", "verified", "recovered", "operation_failed"]) {
    assert.equal(isScheduleInternal(event(status, 0, 0, status)), false, status);
  }
  assert.equal(isScheduleInternal({ event: "network_change" }), false);
});
