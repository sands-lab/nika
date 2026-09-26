import assert from "node:assert/strict";
import test from "node:test";
import {
  annotateLlmRetryAttempts,
  buildLlmTurnDetail,
  closeSupersededOpenSpans,
  collapsePairedEvents,
  displayDurationMs,
  isRunningSpan,
} from "../src/roles.ts";

const event = (id, name, raw = {}) => ({
  id: String(id),
  source: "agent",
  kind: "llm",
  event: name,
  title: name,
  summary: "",
  timestamp: `2026-09-25T12:00:0${id}Z`,
  phase: "diagnosis",
  raw,
});
const ledger = (events) => annotateLlmRetryAttempts(
  closeSupersededOpenSpans(collapsePairedEvents(events)),
);

for (const sameId of [true, false]) {
  test(`Claude thinking/output are completed messages (same message ID: ${sameId})`, () => {
    const rows = ledger([
      event(0, "assistant", { claude_event: { message: {
        id: "msg_1", content: [{ type: "thinking", thinking: "Check routes" }],
      } } }),
      event(1, "assistant", { claude_event: { message: {
        id: sameId ? "msg_1" : "msg_2", content: [{ type: "text", text: "Route missing" }],
      } } }),
    ]);
    assert.deepEqual(rows.map((row) => row.title), ["llm thinking", "llm output"]);
    for (const row of rows) {
      assert.equal(row.attempt, undefined);
      assert.equal(row.stale, undefined);
      assert.equal(row.error, false);
      assert.equal(isRunningSpan(row), false);
      assert.equal(displayDurationMs(row), null);
    }
    const thinking = buildLlmTurnDetail(rows[0], rows);
    assert.equal(thinking.thinking, "Check routes");
    assert.equal(thinking.input, "");
    const output = buildLlmTurnDetail(rows[1], rows);
    assert.equal(output.outputText, "Route missing");
    assert.equal(output.input, "");
  });
}

test("unfinished and failed requests retain retry ordinals", () => {
  for (const failed of [true, false]) {
    const events = [event(0, "llm_start")];
    if (failed) events.push(event(1, "llm_end_error"));
    events.push(event(2, "llm_start"), event(3, "llm_end", { text: "Recovered" }));
    const rows = ledger(events);
    assert.deepEqual(rows.map((row) => row.attempt), [1, 2]);
    assert.equal(rows[0].error, true);
    assert.equal(rows[1].error, false);
    assert.equal(buildLlmTurnDetail(rows[1], rows).outputText, "Recovered");
  }
});

test("HTTP retries retain attempt numbers and active request duration", () => {
  const events = [
    event(0, "llm_start", { run_id: "run_1" }),
    event(1, "llm_retry", { run_id: "run_1" }),
  ];
  const live = ledger(events);
  assert.deepEqual(live.map((row) => row.attempt), [1, 2]);
  assert.equal(isRunningSpan(live[1]), true);
  const done = ledger([...events, event(2, "llm_end", { run_id: "run_1" })]);
  assert.deepEqual(done.map((row) => row.attempt), [1, 2]);
  assert.equal(isRunningSpan(done[1]), false);
});

test("successful requests and standalone messages do not become retries", () => {
  const rows = ledger([
    event(0, "llm_start"), event(1, "llm_end"),
    event(2, "llm_start"), event(3, "llm_end"),
    event(4, "assistant", { text: "Done" }),
  ]);
  assert.deepEqual(rows.map((row) => row.attempt), [undefined, undefined, undefined]);
});
