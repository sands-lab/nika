import { useEffect, useMemo, useState } from "react";
import { api } from "../../api";
import { useAsync } from "../../hooks";
import { useStore } from "../../store";
import { fmtSI, fmtTime } from "../../format";
import { ALERT_COLOR, CHART_THEMES, FAULT_COLOR, SERIES_PALETTE } from "../../theme";
import type { HttpDnsRow, PingmeshData } from "../../types";
import { EChart } from "../charts/EChart";
import { escapeHtml } from "../charts/echarts";
import { TimeChart, type TSeries } from "../charts/TimeChart";
import { Badge, Empty, ErrorState, Loading, Panel, Tabs, Toggle, cx } from "../ui/primitives";

type ProbeTab = "pingmesh" | "http_dns" | "iperf" | "traceroute";

const STATUS: { key: string; label: string; color: string }[] = [
  { key: "ok", label: "ok", color: "#3fca7c" },
  { key: "high_latency", label: "high latency", color: "#e3c04b" },
  { key: "packet_loss", label: "packet loss", color: "#f08a4b" },
  { key: "unreachable", label: "unreachable", color: "#f04452" },
  { key: "policy_violation", label: "policy violation", color: "#b07cf0" },
  { key: "down", label: "down", color: "#d9364a" },
  { key: "isolated", label: "down, isolated by policy", color: "#4f6a9a" },
  { key: "other", label: "other", color: "#8d9bb5" },
];
// A non-ok status on a pair the probe plan expects to be isolated is intended, not an outage.
const cellStatus = (p: PingmeshData["pairs"][number], i: number) => {
  const s = p.status[i];
  return s && s !== "ok" && p.expected?.[i] === "isolated" ? "isolated" : s;
};
const statusIndex = (s: string | null) => {
  if (s === null) return -1;
  const i = STATUS.findIndex((x) => x.key === s);
  return i === -1 ? STATUS.length - 1 : i;
};

function inWindow(t: number, w: [number, number] | null) {
  return !w || (t >= w[0] && t <= w[1]);
}

function Pingmesh({ data }: { data: PingmeshData }) {
  const { theme, range, task, faultMs } = useStore();
  const th = CHART_THEMES[theme];
  const [onlyBad, setOnlyBad] = useState(false);
  const cols = useMemo(() => data.t.map((t, i) => ({ t, i })).filter((c) => inWindow(c.t, range)), [data.t, range]);
  const pairs = useMemo(() => {
    const scored = data.pairs.map((p) => ({ p, bad: p.status.filter((_, i) => { const s = cellStatus(p, i); return s && s !== "ok" && s !== "isolated"; }).length }));
    const filtered = onlyBad ? scored.filter((x) => x.bad > 0) : scored;
    return filtered.sort((a, b) => b.bad - a.bad || `${a.p.source}${a.p.target}`.localeCompare(`${b.p.source}${b.p.target}`)).map((x) => x.p);
  }, [data.pairs, onlyBad]);
  const nBad = data.pairs.filter((p) => p.status.some((_, i) => { const s = cellStatus(p, i); return s && s !== "ok" && s !== "isolated"; })).length;

  const option = useMemo(() => {
    const yLabels = pairs.map((p) => `${p.source} → ${p.target}`);
    const cells: [number, number, number][] = [];
    pairs.forEach((p, y) => cols.forEach((c, x) => cells.push([x, y, statusIndex(cellStatus(p, c.i))])));
    const alertMs = task?.timeline.alert_time_ms;
    const markAt = (ms: number | null | undefined) => {
      if (!ms) return null;
      const idx = cols.findIndex((c) => c.t >= ms);
      return idx === -1 ? null : idx;
    };
    const aIdx = markAt(alertMs);
    const fIdx = markAt(faultMs);
    return {
      animation: false,
      grid: { left: 8, right: 12, top: 8, bottom: 24, containLabel: true },
      tooltip: {
        backgroundColor: th.tooltipBg,
        borderColor: th.tooltipBorder,
        textStyle: { color: th.tooltipText, fontSize: 11 },
        formatter: (p: { value: [number, number, number] }) => {
          const [x, y] = p.value;
          const pair = pairs[y];
          const ci = cols[x].i;
          return `<div style="font-size:11px"><b>${escapeHtml(pair.source)} → ${escapeHtml(pair.target)}</b><br/>${fmtTime(cols[x].t)} UTC<br/>status: <b>${escapeHtml(pair.status[ci] ?? "–")}</b>${pair.expected?.[ci] ? ` (expected: ${escapeHtml(pair.expected[ci] ?? "")})` : ""}<br/>loss: ${pair.loss[ci] ?? "–"}% · rtt: ${pair.rtt[ci] ?? "–"} ms</div>`;
        },
      },
      xAxis: {
        type: "category",
        data: cols.map((c) => fmtTime(c.t)),
        axisLine: { lineStyle: { color: th.axis } },
        axisTick: { show: false },
        axisLabel: { color: th.muted, fontSize: 10, hideOverlap: true },
        splitArea: { show: false },
      },
      yAxis: {
        type: "category",
        data: yLabels,
        inverse: true,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: th.muted, fontSize: 9.5, fontFamily: "JetBrains Mono Variable, monospace" },
      },
      visualMap: {
        type: "piecewise",
        show: false,
        dimension: 2,
        pieces: [{ value: -1, color: th.grid }, ...STATUS.map((s, i) => ({ value: i, color: s.color }))],
      },
      series: [
        {
          type: "heatmap",
          data: cells,
          itemStyle: { borderColor: theme === "dark" ? "#11151d" : "#ffffff", borderWidth: 1, borderRadius: 2 },
          emphasis: { itemStyle: { borderColor: th.text, borderWidth: 1 } },
          markLine: {
            symbol: ["none", "none"],
            silent: true,
            label: { fontSize: 9, position: "start" },
            data: [
              ...(aIdx !== null ? [{ xAxis: aIdx, lineStyle: { color: ALERT_COLOR, type: "dashed" }, label: { formatter: "alert", color: ALERT_COLOR } }] : []),
              ...(fIdx !== null ? [{ xAxis: fIdx, lineStyle: { color: FAULT_COLOR }, label: { formatter: "fault", color: FAULT_COLOR } }] : []),
            ],
          },
        },
      ],
    };
  }, [pairs, cols, th, theme, task, faultMs]);

  const summarySeries = useMemo<TSeries[]>(
    () => [
      { name: "reachable pairs", data: data.t.map((t, i) => [t, data.summary[i]?.reachable_pairs ?? null]), color: "#3fca7c", step: true, area: true },
      { name: "total pairs", data: data.t.map((t, i) => [t, data.summary[i]?.total_pairs ?? null]), color: "#8d9bb5", step: true, dashed: true },
      { name: "anomalies", data: data.t.map((t, i) => [t, data.summary[i]?.anomaly_count ?? null]), color: FAULT_COLOR, step: true },
    ],
    [data],
  );
  const last = data.summary[data.summary.length - 1];
  const minReach = data.summary.reduce((m, s, i) => (s.reachable_pairs < m.v ? { v: s.reachable_pairs, t: data.t[i] } : m), { v: Infinity, t: 0 });

  return (
    <div className="flex h-full min-h-0 flex-col gap-2 overflow-y-auto p-2">
      <Panel
        className="h-[180px] shrink-0"
        title="Reachability summary"
        right={
          last && (
            <span className="flex items-center gap-2 text-[10.5px] normal-case text-fg-3">
              <span>
                last round <b className="text-fg-2">{last.reachable_pairs}/{last.total_pairs}</b> reachable
              </span>
              {Number.isFinite(minReach.v) && minReach.v < (last.total_pairs ?? 0) && (
                <Badge tone="fault">min {minReach.v} at {fmtTime(minReach.t)}</Badge>
              )}
              {last.rtt_median !== null && <span>median rtt {last.rtt_median} ms</span>}
            </span>
          )
        }
      >
        <TimeChart series={summarySeries} legend="right" yMin={0} format={(v) => String(Math.round(v))} />
      </Panel>
      <Panel
        className="shrink-0"
        title={`Pair status over time (${pairs.length} pairs)`}
        right={
          <div className="flex items-center gap-3">
            {STATUS.map((s) => (
              <span key={s.key} className="flex items-center gap-1 text-[10px] normal-case text-fg-3">
                <span className="h-2 w-2 rounded-sm" style={{ background: s.color }} />
                {s.label}
              </span>
            ))}
            <Toggle checked={onlyBad} onChange={setOnlyBad} label={`Only non-ok (${nBad})`} />
          </div>
        }
      >
        {pairs.length === 0 ? (
          <Empty title={onlyBad ? "Every pair stayed ok" : "No pairs"} />
        ) : (
          <div style={{ height: Math.max(120, pairs.length * 13 + 40) }}>
            <EChart option={option} />
          </div>
        )}
      </Panel>
    </div>
  );
}

function HttpDns({ rows }: { rows: HttpDnsRow[] }) {
  const { cats, series, latency } = useMemo(() => {
    const key = (r: HttpDnsRow) => `${r.kind.toUpperCase()} ${r.source} → ${r.target}`;
    const cats = [...new Set(rows.map(key))].sort();
    const idx = new Map(cats.map((c, i) => [c, i]));
    const ok = rows.filter((r) => r.ok);
    const bad = rows.filter((r) => !r.ok);
    const extra = (r: HttpDnsRow) => `${r.code ?? ""}${r.latency_ms !== null ? ` · ${r.latency_ms} ms` : ""}${r.detail && Object.keys(r.detail).length ? ` · ${Object.entries(r.detail).filter(([k]) => k !== "raw").map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(",") : String(v)}`).join(" ")}` : ""}`;
    const series: TSeries[] = [
      { name: "success", type: "scatter", data: ok.map((r) => [r.t, idx.get(key(r))!]), color: "#3fca7c", symbolSize: 7, extra: ok.map(extra) },
      { name: "failure", type: "scatter", data: bad.map((r) => [r.t, idx.get(key(r))!]), color: FAULT_COLOR, symbolSize: 9, extra: bad.map(extra) },
    ];
    const byPair = new Map<string, HttpDnsRow[]>();
    rows.forEach((r) => {
      if (r.latency_ms === null) return;
      const k = key(r);
      if (!byPair.has(k)) byPair.set(k, []);
      byPair.get(k)!.push(r);
    });
    const latency: TSeries[] = [...byPair.entries()].map(([k, rs], i) => ({
      name: k,
      data: rs.sort((a, b) => a.t - b.t).map((r) => [r.t, r.latency_ms]),
      color: SERIES_PALETTE[i % SERIES_PALETTE.length],
      points: true,
    }));
    return { cats, series, latency };
  }, [rows]);
  const nFail = rows.filter((r) => !r.ok).length;
  return (
    <div className="flex h-full min-h-0 flex-col gap-2 overflow-y-auto p-2">
      <Panel
        className="shrink-0"
        title="Probe outcomes"
        right={<span className="text-[10.5px] normal-case text-fg-3">{rows.length} probes · <span className={nFail ? "text-fault" : ""}>{nFail} failed</span></span>}
      >
        <div style={{ height: Math.max(140, cats.length * 18 + 50) }}>
          <TimeChart series={series} yCategories={cats} trigger="item" legend="none" />
        </div>
      </Panel>
      <Panel className="h-[220px] shrink-0" title="Latency (ms)">
        <TimeChart series={latency} legend="right" format={(v) => fmtSI(v, "ms")} />
      </Panel>
    </div>
  );
}

function Iperf() {
  const { taskId } = useStore();
  const st = useAsync(taskId ? () => api.iperf(taskId) : null, [taskId]);
  const series = useMemo<TSeries[]>(() => {
    const rows = st.data?.rows ?? [];
    const pairs = [...new Set(rows.map((r) => `${r.source} → ${r.target}`))].sort();
    return pairs.flatMap((p, i) => {
      const rs = rows.filter((r) => `${r.source} → ${r.target}` === p).sort((a, b) => a.t - b.t);
      const color = SERIES_PALETTE[i % SERIES_PALETTE.length];
      return [
        { name: `${p} (receiver)`, data: rs.map((r) => [r.t, r.ok ? r.receiver_mbps : 0] as [number, number | null]), color, points: true, extra: rs.map((r) => `retx ${r.retransmits ?? "–"}${r.ok ? "" : " · FAILED"}`) },
        { name: `${p} (sender)`, data: rs.map((r) => [r.t, r.ok ? r.sender_mbps : 0] as [number, number | null]), color, points: true, dashed: true },
      ];
    });
  }, [st.data]);
  if (st.error) return <ErrorState error={st.error} />;
  if (!st.data) return <Loading />;
  if (!st.data.rows.length) return <Empty title="No iperf runs recorded" />;
  return (
    <div className="h-full p-2">
      <Panel className="h-full" title="Throughput per pair (Mbit/s; failed runs plotted as 0)">
        <TimeChart series={series} legend="right" yMin={0} format={(v) => `${v.toFixed(1)} Mb/s`} />
      </Panel>
    </div>
  );
}

function Traceroute() {
  const { taskId } = useStore();
  const st = useAsync(taskId ? () => api.traceroute(taskId) : null, [taskId]);
  if (st.error) return <ErrorState error={st.error} />;
  if (!st.data) return <Loading />;
  if (!st.data.length) return <Empty title="No traceroutes recorded" />;
  return (
    <div className="grid h-full auto-rows-min grid-cols-[repeat(auto-fill,minmax(380px,1fr))] gap-2 overflow-y-auto p-2">
      {st.data.map((e, i) => (
        <Panel key={i} title={<span className="font-mono normal-case">{e.source} → {e.target}</span>}>
          <pre className="overflow-x-auto px-3 py-2 font-mono text-[11px] leading-relaxed text-fg-2">{e.output}</pre>
        </Panel>
      ))}
    </div>
  );
}

export function ProbesView() {
  const { taskId, task } = useStore();
  const c = task?.collectors;
  const tabs = [
    { key: "pingmesh" as const, label: "Pingmesh", hidden: !c?.pingmesh },
    { key: "http_dns" as const, label: "HTTP / DNS", hidden: !c?.http_dns },
    { key: "iperf" as const, label: "iperf", hidden: !c?.iperf },
    { key: "traceroute" as const, label: "Traceroute", hidden: !c?.traceroute },
  ];
  const visible = tabs.filter((t) => !t.hidden);
  const [tab, setTab] = useState<ProbeTab>("pingmesh");
  useEffect(() => {
    if (visible.length && !visible.some((t) => t.key === tab)) setTab(visible[0].key);
  }, [visible, tab]);
  const ping = useAsync(taskId && tab === "pingmesh" ? () => api.pingmesh(taskId) : null, [taskId, tab]);
  const hd = useAsync(taskId && tab === "http_dns" ? () => api.httpDns(taskId) : null, [taskId, tab]);
  if (!visible.length) return <Empty title="No probes collected for this task" />;
  return (
    <div className="flex h-full min-h-0 flex-col">
      <Tabs tabs={tabs} value={tab} onChange={setTab} size="sm" className="px-2" />
      <div className={cx("min-h-0 flex-1")}>
        {tab === "pingmesh" &&
          (ping.error ? <ErrorState error={ping.error} /> : !ping.data ? <Loading /> : ping.data.t.length === 0 ? <Empty title="No pingmesh rounds" /> : <Pingmesh data={ping.data} />)}
        {tab === "http_dns" && (hd.error ? <ErrorState error={hd.error} /> : !hd.data ? <Loading /> : hd.data.rows.length === 0 ? <Empty title="No HTTP/DNS probes recorded" /> : <HttpDns rows={hd.data.rows} />)}
        {tab === "iperf" && <Iperf />}
        {tab === "traceroute" && <Traceroute />}
      </div>
    </div>
  );
}
