import { useMemo } from "react";
import { api } from "../../api";
import { useAsync } from "../../hooks";
import { useStore } from "../../store";
import { fmtSI } from "../../format";
import { roleStyle } from "../../theme";
import type { InterfaceData, Segment } from "../../types";
import { TimeChart, zip, type TSeries } from "../charts/TimeChart";
import { Badge, Empty, ErrorState, Loading } from "../ui/primitives";

const RX = "#5b8def";
const TX = "#3fca7c";
const ERR = "#f08a4b";
const DROP = "#ec6fb4";

function sumArrays(a?: (number | null)[], b?: (number | null)[]): (number | null)[] | undefined {
  if (!a && !b) return undefined;
  const n = Math.max(a?.length ?? 0, b?.length ?? 0);
  const out: (number | null)[] = new Array(n);
  for (let i = 0; i < n; i++) {
    const x = a?.[i];
    const y = b?.[i];
    out[i] = (x === null || x === undefined) && (y === null || y === undefined) ? null : (x ?? 0) + (y ?? 0);
  }
  return out;
}

function IfaceCharts({ data }: { data: InterfaceData }) {
  const c = data.counters;
  const charts = useMemo(() => {
    const t = data.t;
    const mk = (name: string, arr: (number | null)[] | undefined, color: string, scale = 1): TSeries[] => (arr ? [{ name, data: zip(t, arr, scale), color }] : []);
    return [
      { title: "Throughput", fmt: (v: number) => fmtSI(v, "bit/s"), series: [...mk("rx", c.rx_bytes, RX, 8), ...mk("tx", c.tx_bytes, TX, 8)] },
      { title: "Packets", fmt: (v: number) => fmtSI(v, "pkt/s"), series: [...mk("rx", c.rx_packets, RX), ...mk("tx", c.tx_packets, TX)] },
      {
        title: "Errors + drops",
        fmt: (v: number) => fmtSI(v, "/s"),
        series: [...mk("errors", sumArrays(c.rx_errs, c.tx_errs), ERR), ...mk("drops", sumArrays(c.rx_drop, c.tx_drop), DROP)],
      },
    ];
  }, [data, c]);
  return (
    <div className="grid grid-cols-1 gap-2">
      {charts.map((ch) => (
        <div key={ch.title} className="rounded-md border border-line bg-surface-2/50">
          <div className="flex items-center justify-between px-2.5 pt-1.5 text-[10px] font-medium text-fg-3">
            <span className="uppercase tracking-wide">{ch.title}</span>
            <span className="flex gap-2">
              {ch.series.map((s) => (
                <span key={s.name} className="flex items-center gap-1">
                  <span className="h-1.5 w-2.5 rounded-sm" style={{ background: s.color }} />
                  {s.name}
                </span>
              ))}
            </span>
          </div>
          <div className="h-[104px]">
            {ch.series.length ? <TimeChart series={ch.series} format={ch.fmt} compact yMin={0} emptyText="All samples missing" /> : <Empty title="Counter not collected" />}
          </div>
        </div>
      ))}
    </div>
  );
}

function EndBlock({ device, port }: { device: string; port: string }) {
  const { taskId, task, select } = useStore();
  const st = useAsync(taskId ? () => api.iface(taskId, device, port) : null, [taskId, device, port]);
  const role = task?.devices.find((d) => d.name === device)?.role;
  return (
    <section className="border-b border-line px-3 py-3">
      <div className="mb-2 flex items-center gap-2">
        <span className="h-2.5 w-2.5 rounded-full" style={{ background: roleStyle(role).color }} />
        <button className="font-mono text-[12px] font-semibold text-fg hover:text-accent hover:underline" onClick={() => select({ kind: "device", name: device })} title="Open device">
          {device}
        </button>
        <span className="font-mono text-[12px] text-fg-3">:{port}</span>
        <span className="flex-1" />
        {st.data?.source && <Badge title={st.data.column ?? undefined}>{st.data.source}</Badge>}
      </div>
      {st.error ? (
        <ErrorState error={st.error} />
      ) : !st.data ? (
        <Loading />
      ) : !st.data.source || st.data.t.length === 0 ? (
        <Empty title="No interface counters" hint={`No Prometheus interface metrics were collected for ${device}:${port}.`} />
      ) : (
        <IfaceCharts data={st.data} />
      )}
    </section>
  );
}

export function LinkPanel({ segment }: { segment: Segment }) {
  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="border-b border-line bg-surface-2 px-3 py-1.5 text-[10.5px] text-fg-3">Per-second rates of interface counters at each end. rx/tx are as seen by that interface.</div>
      {segment.ends.map((e) => (
        <EndBlock key={`${e.device}:${e.port}`} device={e.device} port={e.port} />
      ))}
    </div>
  );
}
