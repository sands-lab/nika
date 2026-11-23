import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { diffLines } from "diff";
import { api } from "../../api";
import { useAsync, useDebounced } from "../../hooks";
import { useStore } from "../../store";
import { fmtTime } from "../../format";
import type { Device } from "../../types";
import { Empty, ErrorState, Loading, TextInput, Toggle, cx } from "../ui/primitives";
import { IconChevron, IconSearch } from "../ui/icons";

function escapeRe(s: string) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Splits text into nodes with <mark> for each match; `counter` tracks global match index. */
function highlight(text: string, re: RegExp | null, counter: { n: number }, current: number): ReactNode {
  if (!re || !text) return text;
  const out: ReactNode[] = [];
  let last = 0;
  re.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m[0].length === 0) {
      re.lastIndex++;
      continue;
    }
    if (m.index > last) out.push(text.slice(last, m.index));
    const idx = counter.n++;
    out.push(
      <mark key={m.index} data-m={idx} className={cx("hl", idx === current && "cur")}>
        {m[0]}
      </mark>,
    );
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

interface DiffLine {
  kind: "add" | "del" | "same";
  text: string;
}

function computeDiff(before: string, after: string): DiffLine[] {
  const parts = diffLines(before, after);
  const out: DiffLine[] = [];
  for (const p of parts) {
    const lines = p.value.replace(/\n$/, "").split("\n");
    for (const l of lines) out.push({ kind: p.added ? "add" : p.removed ? "del" : "same", text: l });
  }
  return out;
}

export function StateView({ device }: { device: Device }) {
  const { taskId, task } = useStore();
  const views = device.views;
  const [view, setView] = useState<string>(() => (views.includes("interfaces") ? "interfaces" : (views[0] ?? "")));
  const [query, setQuery] = useState("");
  const [diffOn, setDiffOn] = useState(false);
  const [onlyChanges, setOnlyChanges] = useState(false);
  const [cur, setCur] = useState(0);
  const q = useDebounced(query, 150);
  const bodyRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!views.includes(view)) setView(views.includes("interfaces") ? "interfaces" : (views[0] ?? ""));
  }, [device.name, views, view]);

  const canDiff = device.before_fault_views.includes(view);
  const showDiff = diffOn && canDiff;
  const snap = useAsync(taskId && view ? () => api.view(taskId, device.name, view, "snapshot") : null, [taskId, device.name, view]);
  const before = useAsync(taskId && view && showDiff ? () => api.view(taskId, device.name, view, "before_fault") : null, [taskId, device.name, view, showDiff]);

  const re = useMemo(() => (q.trim() ? new RegExp(escapeRe(q.trim()), "gi") : null), [q]);
  const diff = useMemo(() => (showDiff && snap.data && before.data ? computeDiff(before.data.text, snap.data.text) : null), [showDiff, snap.data, before.data]);
  const changes = diff ? diff.filter((l) => l.kind !== "same").length : 0;

  const total = useMemo(() => {
    if (!re) return 0;
    const text = diff ? diff.filter((l) => !onlyChanges || l.kind !== "same").map((l) => l.text).join("\n") : (snap.data?.text ?? "");
    return (text.match(re) ?? []).length;
  }, [re, snap.data, diff, onlyChanges]);

  useEffect(() => setCur(0), [q, view, showDiff, onlyChanges]);
  useEffect(() => {
    const el = bodyRef.current?.querySelector<HTMLElement>(`mark[data-m="${cur}"]`);
    el?.scrollIntoView({ block: "center" });
  }, [cur, total, snap.data, diff]);

  const counter = { n: 0 };
  let body: ReactNode;
  if (!view) body = <Empty title="No state views captured for this device" />;
  else if (snap.loading || (showDiff && before.loading)) body = <Loading />;
  else if (snap.error) body = snap.error.message.startsWith("404") ? <Empty title="View not captured" hint={`${view} is missing for ${device.name}.`} /> : <ErrorState error={snap.error} />;
  else if (showDiff && before.error) body = <ErrorState error={before.error} />;
  else if (diff) {
    const lines = onlyChanges ? diff.filter((l) => l.kind !== "same") : diff;
    body =
      changes === 0 ? (
        <Empty title="No differences" hint="The snapshot matches the before-fault capture." />
      ) : (
        <div className="py-1">
          {lines.map((l, i) => (
            <div
              key={i}
              className="flex px-2"
              style={l.kind === "add" ? { background: "var(--add-bg)", color: "var(--add-fg)" } : l.kind === "del" ? { background: "var(--del-bg)", color: "var(--del-fg)" } : undefined}
            >
              <span className="w-4 shrink-0 select-none opacity-60">{l.kind === "add" ? "+" : l.kind === "del" ? "−" : " "}</span>
              <span className="whitespace-pre">{highlight(l.text, re, counter, cur) || " "}</span>
            </div>
          ))}
        </div>
      );
  } else if (snap.data) {
    body = snap.data.text ? <pre className="whitespace-pre px-3 py-2">{highlight(snap.data.text, re, counter, cur)}</pre> : <Empty title="Empty output" />;
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-wrap gap-1 border-b border-line px-3 py-2">
        {views.map((v) => (
          <button
            key={v}
            onClick={() => setView(v)}
            className={cx(
              "rounded px-1.5 py-0.5 font-mono text-[10.5px] transition-colors",
              v === view ? "bg-accent-soft text-accent ring-1 ring-accent/40" : "bg-surface-3 text-fg-2 hover:text-fg",
            )}
          >
            {v}
          </button>
        ))}
      </div>
      <div className="flex items-center gap-2 border-b border-line px-3 py-1.5">
        <TextInput
          className="min-w-0 flex-1"
          value={query}
          onChange={setQuery}
          placeholder={`Search ${view}…`}
          icon={<IconSearch size={12} />}
          right={
            q.trim() ? (
              <div className="flex shrink-0 items-center gap-0.5">
                <span className={cx("mr-1 text-[10px] tabular", total ? "text-fg-2" : "text-fault")}>{total ? `${cur + 1}/${total}` : "0 matches"}</span>
                <button className="rounded p-0.5 text-fg-3 hover:text-fg disabled:opacity-40" disabled={!total} onClick={() => setCur((c) => (c - 1 + total) % total)} title="Previous match">
                  <IconChevron size={12} dir="up" />
                </button>
                <button className="rounded p-0.5 text-fg-3 hover:text-fg disabled:opacity-40" disabled={!total} onClick={() => setCur((c) => (c + 1) % total)} title="Next match">
                  <IconChevron size={12} dir="down" />
                </button>
              </div>
            ) : null
          }
        />
        <Toggle checked={showDiff} onChange={setDiffOn} label="Diff vs before fault" title={canDiff ? "Line diff between the before-fault capture and the snapshot" : "No before-fault capture for this view"} />
        {showDiff && <Toggle checked={onlyChanges} onChange={setOnlyChanges} label="Changes only" />}
      </div>
      {showDiff && diff && (
        <div className="flex items-center gap-3 border-b border-line bg-surface-2 px-3 py-1 text-[10.5px] text-fg-3">
          <span>
            before fault <span className="text-fg-2">→</span> snapshot{task?.timeline.state_snapshot_time_ms ? ` (${fmtTime(task.timeline.state_snapshot_time_ms)})` : ""}
          </span>
          <span style={{ color: "var(--add-fg)" }}>+{diff.filter((l) => l.kind === "add").length}</span>
          <span style={{ color: "var(--del-fg)" }}>−{diff.filter((l) => l.kind === "del").length}</span>
        </div>
      )}
      {!canDiff && diffOn && <div className="border-b border-line bg-surface-2 px-3 py-1 text-[10.5px] text-fg-3">No before-fault capture for this view; showing the snapshot.</div>}
      <div ref={bodyRef} className="min-h-0 flex-1 overflow-auto font-mono text-[11px] leading-[1.55] text-fg-2">
        {body}
      </div>
    </div>
  );
}
