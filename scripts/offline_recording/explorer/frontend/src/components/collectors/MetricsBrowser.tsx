import { useEffect, useMemo, useState } from "react";
import { api } from "../../api";
import { useAsync } from "../../hooks";
import { useStore } from "../../store";
import { roleStyle } from "../../theme";
import type { MetricInfo } from "../../types";
import { MetricChart } from "../charts/MetricChart";
import { MultiSelect, SearchSelect } from "../ui/Select";
import { Badge, Empty, ErrorState, Loading } from "../ui/primitives";

const PREFERRED = [/receive_bytes/, /in_octets/, /bytes/];

export function pickDefaultMetric(list: MetricInfo[]): string | null {
  for (const re of PREFERRED) {
    const m = list.find((x) => re.test(x.metric));
    if (m) return m.metric;
  }
  return list[0]?.metric ?? null;
}

export function useMetricsList() {
  const { taskId, task } = useStore();
  return useAsync(taskId && task?.collectors.prometheus ? () => api.metrics(taskId) : null, [taskId, task?.collectors.prometheus]);
}

export function MetricsBrowser() {
  const { task } = useStore();
  const list = useMetricsList();
  const [metric, setMetric] = useState<string | null>(null);
  const [devices, setDevices] = useState<string[]>([]);
  const info = list.data?.find((m) => m.metric === metric);
  const roleOf = useMemo(() => new Map(task?.devices.map((d) => [d.name, d.role]) ?? []), [task]);

  useEffect(() => {
    if (!list.data) return;
    if (!metric || !list.data.some((m) => m.metric === metric)) setMetric(pickDefaultMetric(list.data));
  }, [list.data, metric]);
  // Keep the device selection valid for the chosen metric; default to every device.
  useEffect(() => {
    if (!info) return;
    setDevices((cur) => {
      const valid = cur.filter((d) => info.devices.includes(d));
      return valid.length ? valid : info.devices;
    });
  }, [info]);

  if (!task?.collectors.prometheus) return <Empty title="Prometheus metrics were not collected for this task" />;
  if (list.error) return <ErrorState error={list.error} />;
  if (!list.data) return <Loading label="Loading metric catalog" />;
  if (!list.data.length) return <Empty title="No metrics" />;
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
        <SearchSelect
          className="w-[380px]"
          options={list.data.map((m) => ({ value: m.metric, hint: `${m.kind} · ${m.n_series}` }))}
          value={metric}
          onChange={setMetric}
          placeholder="Pick a metric"
        />
        {info && (
          <MultiSelect
            className="w-64"
            options={info.devices.map((d) => ({ value: d, color: roleStyle(roleOf.get(d)).color }))}
            value={devices}
            onChange={setDevices}
            placeholder="No devices"
            noun="devices"
          />
        )}
        {info && <Badge tone={info.kind === "counter" ? "accent" : "neutral"}>{info.kind}</Badge>}
        <span className="text-[10.5px] text-fg-3">{list.data.length} metrics</span>
      </div>
      <div className="min-h-0 flex-1 p-2">
        {metric && devices.length ? <MetricChart metric={metric} devices={devices} legend="right" /> : <Empty title="Select at least one device" />}
      </div>
    </div>
  );
}
