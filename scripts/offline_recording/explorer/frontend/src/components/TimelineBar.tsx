import { useEffect, useRef, useState } from "react";
import { useStore } from "../store";
import { fmtDuration, fmtOffset, fmtTime } from "../format";
import { hoverBus } from "./charts/echarts";
import { Button, cx } from "./ui/primitives";
import { IconReset, IconTarget } from "./ui/icons";

type Drag = { mode: "new" | "move" | "l" | "r"; startX: number; orig: [number, number] };

export function TimelineBar() {
  const { task, window: win, setWindow, faultMs } = useStore();
  const trackRef = useRef<HTMLDivElement>(null);
  const [draft, setDraft] = useState<[number, number] | null>(null);
  const [hover, setHover] = useState<number | null>(null);
  const drag = useRef<Drag | null>(null);

  useEffect(() => hoverBus.subscribe((t) => setHover(t)), []);
  useEffect(() => setDraft(null), [win]);

  if (!task) return <div className="h-[46px] border-b border-line bg-surface" />;
  const tl = task.timeline;
  const t0 = tl.recording_start_ms;
  const t1 = tl.recording_end_ms;
  const span = Math.max(1, t1 - t0);
  const toPct = (t: number) => ((t - t0) / span) * 100;
  const clamp = (t: number) => Math.min(t1, Math.max(t0, t));
  const fromClientX = (cx_: number) => {
    const r = trackRef.current!.getBoundingClientRect();
    return t0 + ((cx_ - r.left) / r.width) * span;
  };
  const cur = draft ?? win;

  const onDown = (e: React.PointerEvent, mode: Drag["mode"]) => {
    e.stopPropagation();
    e.preventDefault();
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    const t = clamp(fromClientX(e.clientX));
    drag.current = { mode, startX: t, orig: cur ?? [t, t] };
    if (mode === "new") setDraft([t, t]);
  };
  const onMove = (e: React.PointerEvent) => {
    const t = clamp(fromClientX(e.clientX));
    if (!drag.current) {
      setHover(t);
      return;
    }
    const d = drag.current;
    if (d.mode === "new") setDraft([Math.min(d.startX, t), Math.max(d.startX, t)]);
    else if (d.mode === "move") {
      const w = d.orig[1] - d.orig[0];
      let a = d.orig[0] + (t - d.startX);
      a = Math.min(Math.max(a, t0), t1 - w);
      setDraft([a, a + w]);
    } else if (d.mode === "l") setDraft([Math.min(t, d.orig[1] - 1000), d.orig[1]]);
    else setDraft([d.orig[0], Math.max(t, d.orig[0] + 1000)]);
  };
  const onUp = () => {
    const d = drag.current;
    drag.current = null;
    if (!d) return;
    if (draft && draft[1] - draft[0] >= 2000) setWindow([Math.round(draft[0]), Math.round(draft[1])]);
    else {
      setDraft(null);
      if (d.mode === "new") setWindow(null);
    }
  };

  // ticks every 30s or 60s
  const step = span > 8 * 60_000 ? 60_000 : 30_000;
  const ticks: number[] = [];
  for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) ticks.push(t);

  const markers = [
    { t: tl.alert_time_ms, label: "alert", cls: "bg-alert", text: "text-alert" },
    ...(faultMs ? [{ t: faultMs, label: "fault", cls: "bg-fault", text: "text-fault" }] : []),
    ...(tl.state_snapshot_time_ms ? [{ t: tl.state_snapshot_time_ms, label: "snapshot", cls: "bg-fg-3", text: "text-fg-3" }] : []),
  ];

  return (
    <div className="flex h-[46px] shrink-0 items-center gap-3 border-b border-line bg-surface px-3">
      <div className="flex w-[148px] shrink-0 flex-col leading-tight">
        <span className="text-[10px] font-semibold uppercase tracking-wide text-fg-3">Time window</span>
        <span className="truncate font-mono text-[11px] text-fg-2 tabular">
          {cur ? `${fmtTime(cur[0])}–${fmtTime(cur[1])}` : `${fmtTime(t0)}–${fmtTime(t1)}`}
        </span>
      </div>
      <div className="relative h-full min-w-0 flex-1 py-[7px]">
        <div
          ref={trackRef}
          className="relative h-full cursor-crosshair select-none rounded-md border border-line bg-surface-2"
          onPointerDown={(e) => onDown(e, "new")}
          onPointerMove={onMove}
          onPointerUp={onUp}
          onPointerLeave={() => !drag.current && setHover(null)}
          onDoubleClick={() => setWindow(null)}
          title="Drag to zoom all charts and tables into a window; double-click to reset"
        >
          {/* pre/post-alert shading */}
          <div className="absolute inset-y-0 rounded-r-md bg-alert/[0.06]" style={{ left: `${toPct(tl.alert_time_ms)}%`, right: 0 }} />
          {ticks.filter((t) => toPct(t) < 96).map((t) => (
            <div key={t} className="absolute inset-y-0" style={{ left: `${toPct(t)}%` }}>
              <div className="absolute bottom-0 h-1.5 w-px bg-line-strong" />
              <div className="absolute bottom-[3px] left-1 font-mono text-[9px] text-fg-3 tabular">{step >= 60_000 ? fmtTime(t).slice(0, 5) : fmtTime(t)}</div>
            </div>
          ))}
          {markers.map((m) => (
            <div key={m.label} className="pointer-events-none absolute inset-y-0" style={{ left: `${toPct(m.t)}%` }} title={`${m.label} ${fmtTime(m.t)}`}>
              <div className={cx("absolute inset-y-0 w-[2px] -translate-x-1/2", m.cls)} />
              <div className={cx("absolute top-0.5 whitespace-nowrap text-[9px] font-semibold uppercase tracking-wide", toPct(m.t) > 88 ? "right-1" : "left-1", m.text)}>{m.label}</div>
            </div>
          ))}
          {cur && cur[1] > cur[0] && (
            <>
              <div className="pointer-events-none absolute inset-y-0 left-0 rounded-l-md bg-bg/55" style={{ width: `${toPct(cur[0])}%` }} />
              <div className="pointer-events-none absolute inset-y-0 right-0 rounded-r-md bg-bg/55" style={{ left: `${toPct(cur[1])}%` }} />
              <div
                className="absolute inset-y-0 cursor-grab border-x-2 border-accent bg-accent/10 active:cursor-grabbing"
                style={{ left: `${toPct(cur[0])}%`, width: `${toPct(cur[1]) - toPct(cur[0])}%` }}
                onPointerDown={(e) => onDown(e, "move")}
              >
                <div className="absolute -left-[5px] inset-y-0 w-2 cursor-ew-resize" onPointerDown={(e) => onDown(e, "l")} />
                <div className="absolute -right-[5px] inset-y-0 w-2 cursor-ew-resize" onPointerDown={(e) => onDown(e, "r")} />
              </div>
            </>
          )}
          {hover !== null && hover >= t0 && hover <= t1 && (
            <div className="pointer-events-none absolute inset-y-0 w-px bg-fg-2/60" style={{ left: `${toPct(hover)}%` }}>
              <div className="absolute -top-px left-1 rounded bg-surface-3 px-1 font-mono text-[9px] text-fg-2 tabular">
                {fmtTime(hover)} <span className="text-fg-3">{fmtOffset(hover, tl.alert_time_ms)}</span>
              </div>
            </div>
          )}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        {cur && <span className="font-mono text-[10px] text-fg-3">{fmtDuration(cur[1] - cur[0])}</span>}
        <Button size="xs" title="Zoom to 90 s before … 150 s after the alert" onClick={() => setWindow([Math.max(t0, tl.alert_time_ms - 90_000), Math.min(t1, tl.alert_time_ms + 150_000)])}>
          <IconTarget size={12} /> Alert
        </Button>
        <Button size="xs" disabled={!win} onClick={() => setWindow(null)} title="Show the full recording">
          <IconReset size={12} /> Reset
        </Button>
      </div>
    </div>
  );
}
