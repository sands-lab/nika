import { useMemo, useState } from "react";
import { api } from "../../api";
import { useAsync } from "../../hooks";
import { useStore } from "../../store";
import { fmtInt, fmtSI, fmtTime } from "../../format";
import { SERIES_PALETTE } from "../../theme";
import type { IntSummary } from "../../types";
import { TimeChart, zip, type TSeries } from "../charts/TimeChart";
import { VirtualTable } from "../ui/VirtualTable";
import { Badge, Button, Empty, ErrorState, Loading, Panel } from "../ui/primitives";

const PAGE = 50;

function Records() {
  const { taskId } = useStore();
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState<number | null>(null);
  const st = useAsync(taskId ? () => api.intRecords(taskId, offset, PAGE) : null, [taskId, offset]);
  const total = st.data?.total ?? 0;
  return (
    <Panel
      className="h-full"
      title="Raw INT reports"
      bodyClassName="flex flex-col"
      right={
        <div className="flex items-center gap-1.5 text-[10.5px] normal-case text-fg-3">
          <span className="tabular">
            {total ? `${fmtInt(offset + 1)}–${fmtInt(Math.min(offset + PAGE, total))} of ${fmtInt(total)}` : ""}
          </span>
          <Button size="xs" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
            ‹ Prev
          </Button>
          <Button size="xs" disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>
            Next ›
          </Button>
        </div>
      }
    >
      {st.error ? (
        <ErrorState error={st.error} />
      ) : !st.data ? (
        <Loading />
      ) : st.data.rows.length === 0 ? (
        <Empty title="No INT reports" />
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto">
          {st.data.rows.map((r, i) => {
            const hops = Array.isArray(r.hop_sequence) ? (r.hop_sequence as Record<string, unknown>[]) : [];
            const ts = typeof r.packet_timestamp === "number" ? r.packet_timestamp / 1e6 : null;
            return (
              <div key={offset + i} className="border-b border-line/60">
                <button onClick={() => setOpen(open === i ? null : i)} className="flex w-full items-center gap-3 px-3 py-1 text-left font-mono text-[11px] hover:bg-surface-3/70">
                  <span className="w-[86px] shrink-0 text-fg-3 tabular">{ts ? fmtTime(ts, true) : "–"}</span>
                  <span className="min-w-0 flex-1 truncate text-fg">
                    {String(r.src)}:{String(r.src_port ?? "")} → {String(r.dst)}:{String(r.dst_port ?? "")} <span className="text-accent">p{String(r.protocol ?? "")}</span>
                  </span>
                  <span className="shrink-0 text-fg-3">{hops.map((h) => String(h.switch_id)).join(" → ")}</span>
                </button>
                {open === i && <pre className="max-h-72 overflow-auto bg-surface-2 px-3 py-2 font-mono text-[10.5px] text-fg-2">{JSON.stringify(r, null, 2)}</pre>}
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}

function perSwitch(d: IntSummary, key: "reports" | "hop_latency_avg_us" | "hop_latency_max_us" | "queue_max"): TSeries[] {
  return d.switches.map((s, i) => ({ name: `switch ${s.switch_id}`, data: zip(d.t, s[key]), color: SERIES_PALETTE[i % SERIES_PALETTE.length], points: d.t.length < 30 }));
}

export function IntView() {
  const { taskId, task } = useStore();
  const st = useAsync(taskId ? () => api.intSummary(taskId) : null, [taskId]);
  const d = st.data;
  const charts = useMemo(() => {
    if (!d) return null;
    return {
      rate: perSwitch(d, "reports"),
      lat: perSwitch(d, "hop_latency_avg_us"),
      latMax: perSwitch(d, "hop_latency_max_us"),
      queue: perSwitch(d, "queue_max"),
    };
  }, [d]);
  // INT timestamps come from the switches; if they fall outside the recording window, chart their own extent.
  const outside = useMemo(() => {
    if (!d || !task || !d.t.length) return false;
    const a = d.t[0];
    const b = d.t[d.t.length - 1];
    return b < task.timeline.recording_start_ms || a > task.timeline.recording_end_ms;
  }, [d, task]);
  const xRange: [number, number] | null = outside && d ? [d.t[0] - 1000, d.t[d.t.length - 1] + 1000] : null;

  if (st.error) return <ErrorState error={st.error} />;
  if (!d || !charts) return <Loading label="Loading INT" />;
  if (!d.available) return <Empty title="INT not collected for this task" />;
  const us = (v: number) => fmtSI(v / 1e6, "s");
  return (
    <div className="flex h-full min-h-0 flex-col gap-2 overflow-y-auto p-2">
      <div className="flex items-center gap-2 px-1 text-[11px] text-fg-3">
        <span>
          {fmtInt(d.n_reports)} reports · {d.switches.length} switches · {d.t.length} one-second buckets
        </span>
        {outside && (
          <Badge tone="alert" title="Charts below use the INT data's own time range instead of the global window">
            INT timestamps {fmtTime(d.t[0])}–{fmtTime(d.t[d.t.length - 1])} fall outside the recording window
          </Badge>
        )}
      </div>
      <div className="grid shrink-0 grid-cols-2 gap-2">
        <Panel className="h-[200px]" title="Report rate (reports/s)">
          <TimeChart series={charts.rate} legend="right" xRange={xRange} yMin={0} />
        </Panel>
        <Panel className="h-[200px]" title="Hop latency, average">
          <TimeChart series={charts.lat} legend="right" xRange={xRange} format={us} />
        </Panel>
        <Panel className="h-[200px]" title="Hop latency, max">
          <TimeChart series={charts.latMax} legend="right" xRange={xRange} format={us} />
        </Panel>
        <Panel className="h-[200px]" title="Queue occupancy, max">
          <TimeChart series={charts.queue} legend="right" xRange={xRange} yMin={0} />
        </Panel>
      </div>
      <div className="grid h-[340px] shrink-0 grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)] gap-2">
        <Panel title="Top flows" bodyClassName="flex flex-col">
          <VirtualTable
            className="h-full"
            rows={d.top_flows}
            columns={[
              { key: "src", header: "Source", width: "minmax(0,1fr)", render: (r) => <span className="font-mono">{r.src}</span> },
              { key: "dst", header: "Destination", width: "minmax(0,1fr)", render: (r) => <span className="font-mono">{r.dst}<span className="text-fg-3">:{r.dst_port}</span></span> },
              { key: "p", header: "Proto", width: "44px", render: (r) => <span className="font-mono text-accent">{String(r.protocol)}</span> },
              { key: "h", header: "Path", width: "minmax(0,1fr)", render: (r) => <span className="font-mono text-fg-2">{r.hops}</span> },
              { key: "n", header: "Reports", width: "64px", align: "right", render: (r) => fmtInt(r.reports) },
            ]}
            empty={<Empty title="No flows" />}
          />
        </Panel>
        <Records />
      </div>
    </div>
  );
}
