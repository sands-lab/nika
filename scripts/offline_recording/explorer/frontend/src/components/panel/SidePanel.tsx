import { useEffect, useMemo, useState } from "react";
import { parseComponent, useStore } from "../../store";
import { roleStyle, iconDataUri } from "../../theme";
import type { Device, Segment } from "../../types";
import { useMetricsList } from "../collectors/MetricsBrowser";
import { SyslogView } from "../collectors/SyslogView";
import { NetflowView } from "../collectors/NetflowView";
import { MetricChart } from "../charts/MetricChart";
import { SearchSelect } from "../ui/Select";
import { Badge, Empty, ErrorState, Loading, Tabs } from "../ui/primitives";
import { IconX } from "../ui/icons";
import { StateView } from "./StateView";
import { LinkPanel } from "./LinkPanel";

type DevTab = "state" | "metrics" | "syslog" | "flows";

const PREFERRED = [/network_receive_bytes/, /in_octets/, /bytes/];

function DeviceMetrics({ device }: { device: Device }) {
  const list = useMetricsList();
  const options = useMemo(() => (list.data ?? []).filter((m) => m.devices.includes(device.name)), [list.data, device.name]);
  const [metric, setMetric] = useState<string | null>(null);
  useEffect(() => {
    if (metric && options.some((o) => o.metric === metric)) return;
    let pick: string | null = null;
    for (const re of PREFERRED) {
      pick = options.find((o) => re.test(o.metric))?.metric ?? null;
      if (pick) break;
    }
    setMetric(pick ?? options[0]?.metric ?? null);
  }, [options, metric]);
  if (list.error) return <ErrorState error={list.error} />;
  if (!list.data) return <Loading />;
  if (!options.length) return <Empty title="No metrics for this device" hint="Prometheus did not scrape this device." />;
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 border-b border-line px-3 py-2">
        <SearchSelect className="min-w-0 flex-1" options={options.map((m) => ({ value: m.metric, hint: m.kind }))} value={metric} onChange={setMetric} />
        <span className="shrink-0 text-[10.5px] text-fg-3">{options.length} metrics</span>
      </div>
      <div className="min-h-0 flex-1 p-2">{metric && <MetricChart metric={metric} devices={[device.name]} legend="bottom" />}</div>
    </div>
  );
}

function DeviceBody({ device }: { device: Device }) {
  const { task } = useStore();
  const [tab, setTab] = useState<DevTab>("state");
  // Flows only make sense for devices that exported NetFlow.
  const netflow = !!task?.netflow_exporters?.includes(device.name);
  const tabs = [
    { key: "state" as const, label: `State (${device.views.length})` },
    { key: "metrics" as const, label: "Metrics", hidden: !task?.collectors.prometheus },
    { key: "syslog" as const, label: "Syslog", hidden: !task?.collectors.syslog },
    { key: "flows" as const, label: "Flows", hidden: !netflow },
  ];
  const active = tabs.find((t) => t.key === tab && !t.hidden) ? tab : "state";
  return (
    <>
      <Tabs tabs={tabs} value={active} onChange={setTab} className="px-2" />
      <div className="min-h-0 flex-1">
        {active === "state" && <StateView key={device.name} device={device} />}
        {active === "metrics" && <DeviceMetrics key={device.name} device={device} />}
        {active === "syslog" && <SyslogView key={device.name} device={device.name} />}
        {active === "flows" && <NetflowView key={device.name} exporter={device.name} />}
      </div>
    </>
  );
}

export function SidePanel() {
  const { task, selection, select, label, revealed } = useStore();
  const faultyDevices = useMemo(() => {
    if (!revealed || !label) return new Set<string>();
    return new Set(label.root_causes.flatMap((r) => r.component.split(";").map((c) => parseComponent(c)?.device ?? "")));
  }, [label, revealed]);
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key === "Escape" && (e.target as HTMLElement)?.tagName !== "INPUT") select(null);
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [select]);
  if (!task || !selection) return null;

  let header: React.ReactNode = null;
  let body: React.ReactNode = null;
  if (selection.kind === "device") {
    const d = task.devices.find((x) => x.name === selection.name);
    if (!d) return null;
    const rs = roleStyle(d.role);
    header = (
      <div className="flex min-w-0 items-center gap-2.5">
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg" style={{ background: rs.color }}>
          <img src={iconDataUri(rs.icon)} alt="" className="h-5 w-5" />
        </span>
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="truncate font-mono text-[13px] font-semibold text-fg">{d.name}</span>
            {faultyDevices.has(d.name) && <Badge tone="fault">faulty</Badge>}
          </div>
          <div className="flex flex-wrap items-center gap-1 pt-0.5">
            <span className="text-[11px] text-fg-3">{d.role}</span>
            {d.capabilities.map((c) => (
              <Badge key={c}>{c}</Badge>
            ))}
          </div>
        </div>
      </div>
    );
    body = <DeviceBody key={d.name} device={d} />;
  } else {
    const s: Segment | undefined = task.segments.find((x) => x.id === selection.id);
    if (!s) return null;
    header = (
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="font-mono text-[13px] font-semibold text-fg">{s.id}</span>
          <Badge>{s.ends.length > 2 ? `LAN · ${s.ends.length} members` : s.kind}</Badge>
        </div>
        <div className="truncate pt-0.5 font-mono text-[10.5px] text-fg-3">{s.ends.map((e) => `${e.device}:${e.port}`).join("  ↔  ")}</div>
      </div>
    );
    body = <LinkPanel key={s.id} segment={s} />;
  }
  return (
    <aside className="flex h-full w-[540px] shrink-0 flex-col border-l border-line bg-surface shadow-[-8px_0_24px_-12px_rgba(0,0,0,0.25)]">
      <div className="flex items-start justify-between gap-2 border-b border-line px-3 py-2.5">
        {header}
        <button onClick={() => select(null)} className="rounded-md p-1 text-fg-3 hover:bg-surface-3 hover:text-fg" title="Close (Esc)">
          <IconX size={14} />
        </button>
      </div>
      {body}
    </aside>
  );
}
