import { useMemo, useState } from "react";
import { api } from "../../api";
import { useAsync, useDebounced } from "../../hooks";
import { useStore } from "../../store";
import { fmtBytes, fmtInt, fmtSI, fmtTime } from "../../format";
import { CHART_THEMES, SERIES_PALETTE, roleStyle } from "../../theme";
import type { NetflowData, NetflowRow } from "../../types";
import { EChart } from "../charts/EChart";
import { TimeChart, zip, type TSeries } from "../charts/TimeChart";
import { VirtualTable, type Column } from "../ui/VirtualTable";
import { Empty, ErrorState, Loading, Panel, TextInput } from "../ui/primitives";
import { IconSearch } from "../ui/icons";

const LIMIT = 2000;

function ExporterBars({ data }: { data: NetflowData["by_exporter"] }) {
  const { theme, task } = useStore();
  const th = CHART_THEMES[theme];
  const roleOf = useMemo(() => new Map(task?.devices.map((d) => [d.name, d.role]) ?? []), [task]);
  const sorted = useMemo(() => [...data].sort((a, b) => a.bytes - b.bytes), [data]);
  const option = useMemo(
    () => ({
      animation: false,
      grid: { left: 8, right: 56, top: 6, bottom: 6, containLabel: true },
      tooltip: {
        trigger: "item",
        backgroundColor: th.tooltipBg,
        borderColor: th.tooltipBorder,
        textStyle: { color: th.tooltipText, fontSize: 11 },
        formatter: (p: { name: string; dataIndex: number }) => {
          const r = sorted[p.dataIndex];
          return `<b>${p.name}</b><br/>${fmtBytes(r.bytes)} · ${fmtInt(r.packets)} pkts · ${fmtInt(r.flows)} flows`;
        },
      },
      xAxis: { type: "value", show: false },
      yAxis: {
        type: "category",
        data: sorted.map((d) => d.exporter),
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: th.muted, fontSize: 10, fontFamily: "JetBrains Mono Variable, monospace" },
      },
      series: [
        {
          type: "bar",
          data: sorted.map((d) => ({ value: d.bytes, itemStyle: { color: roleStyle(roleOf.get(d.exporter)).color, borderRadius: [0, 3, 3, 0] } })),
          barMaxWidth: 12,
          label: { show: true, position: "right", color: th.muted, fontSize: 10, formatter: (p: { value: number }) => fmtBytes(p.value) },
        },
      ],
    }),
    [sorted, th, roleOf],
  );
  if (!data.length) return <Empty title="No exporters" />;
  return <EChart option={option} />;
}

export function NetflowView({ exporter }: { exporter?: string }) {
  const { taskId, task, window: win } = useStore();
  const [q, setQ] = useState("");
  const dq = useDebounced(q, 250);
  const st = useAsync(
    taskId ? () => api.netflow(taskId, { exporter, q: dq || undefined, limit: LIMIT, start: win?.[0], end: win?.[1] }) : null,
    [taskId, exporter, dq, win?.[0], win?.[1]],
  );
  const roleOf = useMemo(() => new Map(task?.devices.map((d) => [d.name, d.role]) ?? []), [task]);
  const d = st.data;
  const series = useMemo<TSeries[]>(() => {
    if (!d) return [];
    return d.timeseries.series.map((s, i) => ({
      name: s.exporter,
      data: zip(d.timeseries.t, s.values, 8),
      area: true,
      stack: "bytes",
      color: exporter ? SERIES_PALETTE[0] : SERIES_PALETTE[i % SERIES_PALETTE.length],
    }));
  }, [d, exporter]);
  const rows = useMemo(() => [...(d?.rows ?? [])].sort((a, b) => a.first_ms - b.first_ms), [d]);

  const rawCols: Column<NetflowRow>[] = [
    { key: "first", header: "First", width: "70px", render: (r) => <span className="font-mono text-fg-2 tabular">{fmtTime(r.first_ms)}</span> },
    { key: "last", header: "Last", width: "70px", render: (r) => <span className="font-mono text-fg-3 tabular">{fmtTime(r.last_ms)}</span> },
    ...(!exporter
      ? [
          {
            key: "exp",
            header: "Exporter",
            width: "minmax(90px,1fr)",
            render: (r: NetflowRow) => (
              <span className="flex items-center gap-1.5 font-mono">
                <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: roleStyle(roleOf.get(r.exporter)).color }} />
                <span className="truncate">{r.exporter}</span>
              </span>
            ),
          },
        ]
      : []),
    { key: "if", header: "In if", width: "64px", render: (r) => <span className="font-mono text-fg-3">{r.in_if}</span> },
    { key: "proto", header: "Proto", width: "46px", render: (r) => <span className="font-mono text-accent">{r.proto}</span> },
    { key: "src", header: "Source", width: "minmax(110px,1.2fr)", render: (r) => <span className="font-mono">{r.src_ip}{r.src_port ? <span className="text-fg-3">:{r.src_port}</span> : null}</span> },
    { key: "dst", header: "Destination", width: "minmax(110px,1.2fr)", render: (r) => <span className="font-mono">{r.dst_ip}{r.dst_port ? <span className="text-fg-3">:{r.dst_port}</span> : null}</span> },
    { key: "pk", header: "Pkts", width: "56px", align: "right", render: (r) => fmtInt(r.packets) },
    { key: "by", header: "Bytes", width: "72px", align: "right", render: (r) => fmtBytes(r.bytes) },
  ];

  const toolbar = (
    <div className="flex items-center gap-2 border-b border-line px-3 py-2">
      <TextInput className="min-w-40 flex-1" value={q} onChange={setQ} placeholder="Filter by IP, port or protocol…" icon={<IconSearch size={12} />} />
      {d && (
        <span className="text-[10.5px] text-fg-3 tabular">
          {fmtInt(d.matched)} matched{d.matched > rows.length ? ` · showing ${fmtInt(rows.length)}` : ""} · {fmtInt(d.total)} total{win ? " · in window" : ""}
        </span>
      )}
    </div>
  );

  if (st.error) return <ErrorState error={st.error} />;
  if (!d) return <Loading label="Loading NetFlow" />;
  const rawTable = (
    <VirtualTable className="h-full" rows={rows} columns={rawCols} empty={<Empty title="No flow records" hint={q || win ? "Nothing matches the filter or time window." : undefined} />} />
  );
  const chart = <TimeChart series={series} format={(v) => fmtSI(v, "bit/s")} legend={exporter ? "none" : "right"} yMin={0} emptyText="No flow volume" />;

  if (exporter) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        {toolbar}
        <div className="h-[170px] shrink-0 border-b border-line">{chart}</div>
        <div className="min-h-0 flex-1">{rawTable}</div>
      </div>
    );
  }
  return (
    <div className="flex h-full min-h-0 flex-col">
      {toolbar}
      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        <div className="grid min-h-[230px] grid-cols-[minmax(0,2fr)_minmax(0,1fr)] gap-2" style={{ height: "45%" }}>
          <Panel title={`Bit/s per exporter (${d.timeseries.bucket_s}s buckets, stacked)`}>{chart}</Panel>
          <Panel title="Bytes by exporter">
            <ExporterBars data={d.by_exporter} />
          </Panel>
        </div>
        <div className="mt-2 grid min-h-[260px] grid-cols-[minmax(0,1fr)_minmax(0,1.6fr)] gap-2" style={{ height: "55%" }}>
          <Panel title="Top talkers" bodyClassName="flex flex-col">
            <VirtualTable
              className="h-full"
              rows={d.top_talkers}
              columns={[
                { key: "src", header: "Source", width: "minmax(0,1fr)", render: (r) => <span className="font-mono">{r.src_ip}</span> },
                { key: "dst", header: "Destination", width: "minmax(0,1fr)", render: (r) => <span className="font-mono">{r.dst_ip}<span className="text-fg-3">{r.dst_port ? `:${r.dst_port}` : ""}</span></span> },
                { key: "p", header: "Proto", width: "44px", render: (r) => <span className="font-mono text-accent">{r.proto}</span> },
                { key: "b", header: "Bytes", width: "70px", align: "right", render: (r) => fmtBytes(r.bytes) },
                { key: "f", header: "Flows", width: "48px", align: "right", render: (r) => fmtInt(r.flows) },
              ]}
              empty={<Empty title="No talkers" />}
            />
          </Panel>
          <Panel title="Flow records" bodyClassName="flex flex-col">
            {rawTable}
          </Panel>
        </div>
      </div>
    </div>
  );
}
