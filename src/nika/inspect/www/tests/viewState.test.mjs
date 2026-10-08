import assert from "node:assert/strict";
import test from "node:test";
import {
  buildUrlSearch,
  matchesNumericFilter,
  parseNumericFilter,
  parseUrlState,
} from "../src/viewState.ts";

test("deep link round-trips root, session, tab, source, event, and find", () => {
  const state = {
    root: "bench run/trials",
    session: "run-a/trials/t01",
    tab: "raw",
    source: "agent",
    event: "agent-12",
    find: "show bgp",
  };
  assert.deepEqual(parseUrlState(buildUrlSearch(state)), state);
});

test("default tab and root are omitted from the URL", () => {
  assert.equal(
    buildUrlSearch({
      root: ".",
      session: "s1",
      tab: "timeline",
      source: null,
      event: null,
      find: null,
    }),
    "?session=s1",
  );
  assert.equal(
    buildUrlSearch({
      root: null,
      session: null,
      tab: "raw",
      source: "nika",
      event: "x",
      find: "y",
    }),
    "",
  );
});

test("session-only fields and unknown tabs are dropped when parsing", () => {
  assert.deepEqual(parseUrlState("?tab=raw&source=agent&event=agent-1&find=x"), {
    root: null,
    session: null,
    tab: null,
    source: null,
    event: null,
    find: null,
  });
  assert.equal(parseUrlState("?session=s&tab=bogus").tab, null);
  assert.equal(parseUrlState("?session=s&source=bogus").source, null);
});

test("legacy agent / nika tabs open the timeline with that source", () => {
  const nika = parseUrlState("?session=s&tab=nika");
  assert.equal(nika.tab, "timeline");
  assert.equal(nika.source, "nika");
  assert.equal(parseUrlState("?session=s&tab=agent").source, "agent");
});

test("numeric filter expressions", () => {
  assert.deepEqual(parseNumericFilter("<0.5"), { op: "<", value: 0.5 });
  assert.deepEqual(parseNumericFilter(" >= 30 "), { op: ">=", value: 30 });
  assert.deepEqual(parseNumericFilter("1"), { op: "=", value: 1 });
  assert.deepEqual(parseNumericFilter("!=.25"), { op: "!=", value: 0.25 });
  assert.equal(parseNumericFilter(""), null);
  assert.equal(parseNumericFilter("<"), null);
  assert.equal(parseNumericFilter("abc"), null);
});

test("missing values never match a numeric filter", () => {
  const lt = parseNumericFilter("<0.5");
  assert.equal(matchesNumericFilter(0.2, lt), true);
  assert.equal(matchesNumericFilter(0.5, lt), false);
  assert.equal(matchesNumericFilter(null, lt), false);
  assert.equal(matchesNumericFilter(undefined, parseNumericFilter("!=1")), false);
});
