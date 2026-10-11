import assert from "node:assert/strict";
import test from "node:test";
import {
  mergeIntervals,
  sessionTimeBreakdown,
  slowestOperations,
  toolUsage,
  unionMs,
} from "../src/analysis.ts";
import {
  closeSupersededOpenSpans,
  collapsePairedEvents,
  llmDurationStats,
} from "../src/roles.ts";

const T0 = Date.parse("2026-09-25T12:00:00Z");
const iso = (offsetMs) => new Date(T0 + offsetMs).toISOString();

let seq = 0;
const nika = (name, offsetMs, durationMs = null) => ({
  id: `nika-${seq++}`,
  source: "nika",
  kind: "lifecycle",
  event: name,
  title: name,
  summary: "",
  timestamp: iso(offsetMs),
  duration_ms: durationMs,
  raw: {},
});
const agent = (name, offsetMs, extra = {}) => ({
  id: `agent-${seq++}`,
  source: "agent",
  kind: extra.kind ?? "llm",
  event: name,
  title: name,
  summary: "",
  timestamp: iso(offsetMs),
  phase: "diagnosis",
  raw: { run_id: extra.run_id },
  tool: extra.tool,
});
const toolCall = (name, offsetMs, id) =>
  agent("tool_start", offsetMs, {
    kind: "tool_call",
    tool: { name, tool_call_id: id },
  });
const toolEnd = (name, offsetMs, id, error = false) =>
  agent(error ? "tool_error" : "tool_end", offsetMs, {
    kind: error ? "tool_error" : "tool_result",
    tool: { name, tool_call_id: id, error: error ? "boom" : null },
  });

/**
 * One finished session: 60 s lab setup, 2 s inject, 40 s agent run
 * (5 s sandbox, two 10 s LLM requests, one 5 s tool), 4 s teardown.
 */
function finishedSession() {
  seq = 0;
  return [
    nika("env_start", 60_000, 60_000),
    nika("failure_inject_complete", 62_000, 2_000),
    nika("agent_start", 63_000),
    nika("sandbox_start", 68_000, 5_000),
    agent("llm_start", 68_000, { run_id: "r1" }),
    agent("llm_end", 78_000, { run_id: "r1" }),
    toolCall("nika_exec", 78_000, "c1"),
    toolEnd("nika_exec", 83_000, "c1"),
    agent("llm_start", 83_000, { run_id: "r2" }),
    agent("llm_end", 93_000, { run_id: "r2" }),
    nika("agent_end", 103_000, 40_000),
    nika("env_stop", 108_000, 4_000),
  ];
}

test("mergeIntervals unions overlaps and clips to a window", () => {
  const merged = mergeIntervals(
    [
      { start: 10, end: 20 },
      { start: 15, end: 30 },
      { start: 40, end: 50 },
    ],
    { start: 12, end: 45 },
  );
  assert.deepEqual(merged, [
    { start: 12, end: 30 },
    { start: 40, end: 45 },
  ]);
  assert.equal(unionMs([{ start: 0, end: 10 }, { start: 5, end: 15 }]), 15);
});

test("finished session splits wall clock into phases and the agent run into llm/tools/overhead", () => {
  const events = finishedSession();
  const bd = sessionTimeBreakdown(events, { live: false });
  assert.ok(bd);
  assert.equal(bd.totalMs, 108_000);
  assert.equal(bd.live, false);
  const session = Object.fromEntries(bd.session.map((s) => [s.key, s.ms]));
  assert.deepEqual(session, {
    setup: 60_000,
    inject: 2_000,
    agent: 40_000,
    teardown: 4_000,
    other: 2_000,
  });
  assert.equal(bd.agentMs, 40_000);
  const agentShares = Object.fromEntries(bd.agent.map((s) => [s.key, s.ms]));
  assert.deepEqual(agentShares, {
    llm: 20_000,
    tools: 5_000,
    sandbox: 5_000,
    overhead: 10_000,
  });
  const sum = bd.session.reduce((a, s) => a + s.pct, 0);
  assert.ok(Math.abs(sum - 1) < 1e-9, "session shares sum to 100%");
  assert.ok(Math.abs(bd.agent.reduce((a, s) => a + s.pct, 0) - 1) < 1e-9);
});

test("explicit session bounds extend the window; empty logs yield null", () => {
  const events = finishedSession();
  const bd = sessionTimeBreakdown(events, {
    live: false,
    startMs: T0 - 10_000,
    endMs: T0 + 120_000,
  });
  assert.equal(bd.totalMs, 130_000);
  assert.equal(bd.session.find((s) => s.key === "other").ms, 24_000);
  assert.equal(sessionTimeBreakdown([], { live: false }), null);
});

test("live session counts open llm request and agent run up to now", () => {
  seq = 0;
  const events = [
    nika("env_start", 60_000, 60_000),
    nika("agent_start", 61_000),
    agent("llm_start", 61_000, { run_id: "r1" }),
  ];
  const bd = sessionTimeBreakdown(events, { live: true, nowMs: T0 + 71_000 });
  assert.equal(bd.live, true);
  assert.equal(bd.totalMs, 71_000);
  assert.equal(bd.session.find((s) => s.key === "agent").ms, 10_000);
  assert.equal(bd.agentMs, 10_000);
  assert.equal(bd.agent.find((s) => s.key === "llm").ms, 10_000);
  assert.equal(bd.agent.find((s) => s.key === "overhead"), undefined);
});

test("overlapping phases (teardown logged inside a live agent window) still sum to 100%", () => {
  seq = 0;
  const events = [
    nika("env_start", 60_000, 60_000),
    nika("agent_start", 61_000),
    // Watchdog tore the lab down while the agent window is still open.
    nika("env_stop", 100_000, 5_000),
  ];
  const bd = sessionTimeBreakdown(events, { live: true, nowMs: T0 + 120_000 });
  assert.equal(bd.totalMs, 120_000);
  assert.equal(bd.session.reduce((a, s) => a + s.ms, 0), bd.totalMs);
  const session = Object.fromEntries(bd.session.map((s) => [s.key, s.ms]));
  assert.deepEqual(session, { setup: 60_000, agent: 59_000, other: 1_000 });
  assert.equal(bd.agentMs, 59_000);
});

test("live: an abandoned llm_start ends where the next request starts", () => {
  seq = 0;
  const events = [
    nika("agent_start", 0),
    agent("llm_start", 0, { run_id: "r1" }),
    agent("llm_start", 5_000, { run_id: "r2" }),
    agent("llm_end", 8_000, { run_id: "r2" }),
    toolCall("ping", 8_000, "c1"),
    toolEnd("ping", 9_000, "c1"),
  ];
  const bd = sessionTimeBreakdown(events, { live: true, nowMs: T0 + 20_000 });
  const agentShares = Object.fromEntries(bd.agent.map((s) => [s.key, s.ms]));
  assert.deepEqual(agentShares, { llm: 8_000, tools: 1_000, overhead: 11_000 });
});

test("tool calls inside an llm turn (Codex) count as tool time, not llm time", () => {
  seq = 0;
  const events = [
    nika("agent_start", 0),
    agent("llm_start", 0, { run_id: "r1" }),
    toolCall("ping", 2_000, "c1"),
    toolEnd("ping", 12_000, "c1"),
    agent("llm_end", 8_000, { run_id: "r1" }),
    nika("agent_end", 12_000, 12_000),
  ];
  const bd = sessionTimeBreakdown(events, { live: false });
  const agentShares = Object.fromEntries(bd.agent.map((s) => [s.key, s.ms]));
  assert.deepEqual(agentShares, { llm: 2_000, tools: 10_000 });
});

test("toolUsage groups by tool with error counts and timing, busiest first", () => {
  seq = 0;
  const rows = closeSupersededOpenSpans(
    collapsePairedEvents([
      toolCall("nika_exec", 0, "a"),
      toolEnd("nika_exec", 3_000, "a"),
      toolCall("read_file", 3_000, "b"),
      toolEnd("read_file", 3_500, "b", true),
      toolCall("nika_exec", 4_000, "c"),
      toolEnd("nika_exec", 5_000, "c"),
    ]),
  );
  const usage = toolUsage(rows);
  assert.deepEqual(
    usage.map((u) => [u.name, u.calls, u.errors, u.totalMs, u.avgMs, u.maxMs]),
    [
      ["nika_exec", 2, 0, 4_000, 2_000, 3_000],
      ["read_file", 1, 1, 500, 500, 500],
    ],
  );
});

test("slowestOperations ranks ledger rows and skips run-length bookends", () => {
  const events = finishedSession();
  const rows = closeSupersededOpenSpans(collapsePairedEvents(events));
  const slow = slowestOperations(rows, 3);
  assert.deepEqual(
    slow.map((s) => [s.label, s.durationMs]),
    [
      ["env start", 60_000],
      ["llm", 10_000],
      ["llm", 10_000],
    ],
  );
  assert.ok(slow.every((s) => rows.some((r) => r.id === s.rowId)));
  const all = slowestOperations(rows, 50);
  assert.ok(!all.some((s) => s.label === "agent end"), "agent_end is not an operation");
});

test("llmDurationStats reports the median request time", () => {
  const events = finishedSession();
  const rows = closeSupersededOpenSpans(collapsePairedEvents(events));
  const stats = llmDurationStats(rows);
  assert.equal(stats.n, 2);
  assert.equal(stats.p50Ms, 10_000);
  assert.equal(llmDurationStats([]).p50Ms, null);
});
