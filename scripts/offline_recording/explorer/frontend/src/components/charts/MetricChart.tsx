import { useMemo } from "react";
import { api } from "../../api";
import { useAsync } from "../../hooks";
import { useStore } from "../../store";
import { fmtSI, metricUnit } from "../../format";
import type { MetricSeries } from "../../types";
import { ErrorState, Loading } from "../ui/primitives";
import { TimeChart, zip, type TSeries } from "./TimeChart";

export function seriesName(s: MetricSeries, withDevice: boolean): string {
  const lbl = Object.entries(s.labels)
    .map(([k, v]) => `${k}=${v}`)
    .join(",");
  if (withDevice) return lbl ? `${s.device}{${lbl}}` : s.device;
  return lbl || s.device || s.id;
}

/** Fetches one Prometheus metric for the given devices and charts every series. */
export function MetricChart({ metric, devices, legend = "bottom" }: { metric: string; devices: string[]; legend?: "bottom" | "right" | "none" }) {
  const { taskId } = useStore();
  const st = useAsync(taskId && metric && devices.length ? () => api.metric(taskId, metric, devices) : null, [taskId, metric, devices.join(",")]);
  const multiDevice = devices.length > 1;
  const { series, format, info } = useMemo(() => {
    const d = st.data;
    if (!d) return { series: [] as TSeries[], format: (v: number) => fmtSI(v), info: "" };
    const u = metricUnit(d.metric, d.rate);
    const series: TSeries[] = d.series.map((s) => ({ name: seriesName(s, multiDevice), data: zip(d.t, s.values, u.scale) }));
    return {
      series,
      format: (v: number) => fmtSI(v, u.unit),
      info: `${d.kind}${d.rate ? " · per-second rate" : ""} · ${d.series.length} series`,
    };
  }, [st.data, multiDevice]);

  if (!devices.length) return null;
  if (st.error) return <ErrorState error={st.error} />;
  if (!st.data) return <Loading label="Loading metric" />;
  return (
    <div className="relative h-full w-full">
      <TimeChart series={series} format={format} legend={series.length > 1 ? legend : "none"} emptyText="No samples in this metric" />
      <div className="pointer-events-none absolute right-3 top-0 text-[10px] text-fg-3">{info}</div>
    </div>
  );
}
