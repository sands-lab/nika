export type SessionTab = "overview" | "timeline" | "agent" | "nika" | "scores" | "raw";

const SESSION_TABS: readonly SessionTab[] = [
  "overview",
  "timeline",
  "agent",
  "nika",
  "scores",
  "raw",
];

/** Viewer location encoded in the page query string (shareable deep link). */
export interface UrlState {
  root: string | null;
  session: string | null;
  tab: SessionTab | null;
  event: string | null;
  find: string | null;
}

export function parseUrlState(search: string): UrlState {
  const params = new URLSearchParams(search);
  const text = (key: string) => params.get(key)?.trim() || null;
  const tab = text("tab");
  const session = text("session");
  return {
    root: text("root"),
    session,
    tab: session && SESSION_TABS.includes(tab as SessionTab) ? (tab as SessionTab) : null,
    event: session ? text("event") : null,
    find: session ? text("find") : null,
  };
}

/** Query string (with leading ``?``, or empty) for ``state``; session fields need a session. */
export function buildUrlSearch(state: UrlState): string {
  const params = new URLSearchParams();
  if (state.root && state.root !== ".") params.set("root", state.root);
  if (state.session) {
    params.set("session", state.session);
    if (state.tab && state.tab !== "timeline") params.set("tab", state.tab);
    if (state.event) params.set("event", state.event);
    if (state.find) params.set("find", state.find);
  }
  const out = params.toString();
  return out ? `?${out}` : "";
}

export type NumericOp = "<" | "<=" | ">" | ">=" | "=" | "!=";

export interface NumericFilter {
  op: NumericOp;
  value: number;
}

/** Parse ``<0.5`` / ``>= 30`` / ``1`` (means ``=``); ``null`` when blank or invalid. */
export function parseNumericFilter(text: string): NumericFilter | null {
  const m = text.trim().match(/^(<=|>=|!=|<|>|=)?\s*(-?\d+(?:\.\d+)?|-?\.\d+)$/);
  if (!m) return null;
  return { op: (m[1] as NumericOp | undefined) ?? "=", value: Number(m[2]) };
}

/** Missing values never match, so a filter narrows the list to scored rows. */
export function matchesNumericFilter(
  value: number | null | undefined,
  filter: NumericFilter,
): boolean {
  if (value == null || !Number.isFinite(value)) return false;
  switch (filter.op) {
    case "<":
      return value < filter.value;
    case "<=":
      return value <= filter.value;
    case ">":
      return value > filter.value;
    case ">=":
      return value >= filter.value;
    case "=":
      return value === filter.value;
    case "!=":
      return value !== filter.value;
  }
}
