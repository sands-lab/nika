import { useCallback, useMemo } from "react";
import { useStore } from "../../store";
import { ALERT_COLOR, CHART_THEMES, FAULT_COLOR, SERIES_PALETTE } from "../../theme";
import { fmtSI, fmtTime } from "../../format";
import { EChart } from "./EChart";
import { echarts, escapeHtml, hoverBus, type ECOption } from "./echarts";
import { Empty } from "../ui/primitives";

export type Point = [number, number | null];

export interface TSeries {
  name: string;
  data: Point[];
  color?: string;
  type?: "line" | "scatter" | "bar";
  area?: boolean;
  stack?: string;
  step?: boolean;
  symbolSize?: number;
  dashed?: boolean;
  /** Draw point markers on a line series (sparse data). */
  points?: boolean;
  /** Extra tooltip text per data index (scatter). */
  extra?: string[];
}

export function zip(t: number[], values: (number | null)[], scale = 1): Point[] {
  const out: Point[] = new Array(t.length);
  for (let i = 0; i < t.length; i++) {
    const v = values[i];
    out[i] = [t[i], v === null || v === undefined ? null : v * scale];
  }
  return out;
}

export interface TimeChartProps {
  series: TSeries[];
  format?: (v: number) => string;
  legend?: "bottom" | "right" | "none";
  xRange?: [number, number] | null;
  yMin?: number;
  yMax?: number;
  emptyText?: string;
  trigger?: "axis" | "item";
  compact?: boolean;
  className?: string;
  maxTooltipRows?: number;
  yCategories?: string[];
}

function gridRect(chart: echarts.ECharts): { x: number; y: number; width: number; height: number } | null {
  try {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const grid = (chart as any).getModel().getComponent("grid", 0);
    return grid?.coordinateSystem?.getRect() ?? null;
  } catch {
    return null;
  }
}

export function TimeChart({
  series,
  format = (v) => fmtSI(v),
  legend = "none",
  xRange,
  yMin,
  yMax,
  emptyText = "No data",
  trigger = "axis",
  compact = false,
  className,
  maxTooltipRows = 14,
  yCategories,
}: TimeChartProps) {
  const { theme, task, faultMs, range } = useStore();
  const th = CHART_THEMES[theme];
  const alertMs = task?.timeline.alert_time_ms ?? null;
  const x = xRange ?? range;

  const option = useMemo<ECOption>(() => {
    const marks: object[] = [];
    if (alertMs) marks.push({ xAxis: alertMs, lineStyle: { color: ALERT_COLOR, type: "dashed", width: 1.2 }, label: { formatter: "alert", color: ALERT_COLOR } });
    if (faultMs) marks.push({ xAxis: faultMs, lineStyle: { color: FAULT_COLOR, type: "solid", width: 1.4 }, label: { formatter: "fault", color: FAULT_COLOR } });
    const legendOn = legend !== "none" && series.length > 0;
    const s = series.map((sr, i) => {
      const color = sr.color ?? SERIES_PALETTE[i % SERIES_PALETTE.length];
      const type = sr.type ?? "line";
      return {
        name: sr.name,
        type,
        data: sr.data,
        stack: sr.stack,
        step: sr.step ? "end" : undefined,
        showSymbol: type === "scatter" || !!sr.points,
        connectNulls: !!sr.points,
        symbolSize: sr.symbolSize ?? (type === "scatter" ? 7 : 4),
        sampling: type === "line" ? "lttb" : undefined,
        barMaxWidth: 14,
        itemStyle: { color },
        lineStyle: { width: 1.4, color, type: sr.dashed ? "dashed" : "solid" },
        areaStyle: sr.area ? { opacity: sr.stack ? 0.55 : 0.15, color } : undefined,
        emphasis: { focus: "none", lineStyle: { width: 2 } },
        animation: false,
      };
    });
    s.push({
      name: "__markers",
      type: "line",
      data: [],
      markLine: {
        symbol: ["none", "none"],
        silent: true,
        animation: false,
        label: { position: "insideEndTop", fontSize: 10, fontWeight: 600, distance: 2 },
        data: marks,
      },
    } as never);

    const tooltipFormatter = (params: unknown) => {
      const ps = (Array.isArray(params) ? params : [params]) as { seriesName: string; value: [number, number | null]; color: string; dataIndex: number; seriesIndex: number }[];
      const items = ps.filter((p) => p.seriesName !== "__markers" && p.value && p.value[1] !== null && p.value[1] !== undefined);
      if (items.length === 0) return "";
      const t = items[0].value[0];
      if (yCategories) {
        return `<div style="font-size:11px"><b>${fmtTime(t)}</b>${items
          .map((p) => {
            const ex = series[p.seriesIndex]?.extra?.[p.dataIndex];
            return `<div><span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:${p.color};margin-right:6px"></span>${escapeHtml(p.seriesName)}${ex ? `<div style="opacity:.8;margin-left:13px;max-width:360px;white-space:normal">${escapeHtml(ex)}</div>` : ""}</div>`;
          })
          .join("")}</div>`;
      }
      const sorted = [...items].sort((a, b) => (b.value[1] as number) - (a.value[1] as number));
      const shown = sorted.slice(0, maxTooltipRows);
      const more = sorted.length - shown.length;
      const rows = shown
        .map((p) => {
          const ex = series[p.seriesIndex]?.extra?.[p.dataIndex];
          return `<tr><td style="padding:1px 8px 1px 0;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"><span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:${p.color};margin-right:6px"></span>${escapeHtml(p.seriesName)}${ex ? ` <span style="opacity:.7">${escapeHtml(ex)}</span>` : ""}</td><td style="text-align:right;font-variant-numeric:tabular-nums;font-weight:600">${escapeHtml(format(p.value[1] as number))}</td></tr>`;
        })
        .join("");
      return `<div style="font-size:11px"><div style="margin-bottom:3px;opacity:.75">${fmtTime(t, true)} UTC</div><table>${rows}</table>${more > 0 ? `<div style="opacity:.6;margin-top:2px">+${more} more</div>` : ""}</div>`;
    };

    const legendCfg = !legendOn
      ? { show: false }
      : legend === "right"
        ? { show: true, type: "scroll", orient: "vertical", right: 4, top: 18, bottom: 6, width: 170, data: series.map((x) => x.name), textStyle: { color: th.muted, fontSize: 10, width: 150, overflow: "truncate" }, pageTextStyle: { color: th.muted }, pageIconColor: th.text, pageIconInactiveColor: th.axis, itemWidth: 10, itemHeight: 6, icon: "roundRect" }
        : { show: true, type: "scroll", bottom: 0, left: 8, right: 8, data: series.map((x) => x.name), textStyle: { color: th.muted, fontSize: 10 }, pageTextStyle: { color: th.muted }, pageIconColor: th.text, pageIconInactiveColor: th.axis, itemWidth: 10, itemHeight: 6, icon: "roundRect" };

    return {
      backgroundColor: th.bg,
      animation: false,
      textStyle: { fontFamily: "Inter Variable, system-ui, sans-serif" },
      grid: {
        left: compact ? 6 : 10,
        right: legendOn && legend === "right" ? 190 : compact ? 10 : 16,
        top: compact ? 14 : 18,
        bottom: legendOn && legend === "bottom" ? 28 : compact ? 4 : 6,
        containLabel: true,
      },
      tooltip: {
        trigger,
        confine: true,
        backgroundColor: th.tooltipBg,
        borderColor: th.tooltipBorder,
        textStyle: { color: th.tooltipText, fontSize: 11 },
        extraCssText: "box-shadow:0 8px 24px rgba(0,0,0,.25);border-radius:6px;",
        axisPointer: { type: "line", lineStyle: { color: th.crosshair, width: 1 }, animation: false },
        formatter: tooltipFormatter,
        transitionDuration: 0,
      },
      legend: legendCfg,
      xAxis: {
        type: "time",
        splitNumber: compact ? 4 : 6,
        min: x ? x[0] : undefined,
        max: x ? x[1] : undefined,
        axisLine: { lineStyle: { color: th.axis } },
        axisTick: { show: false },
        splitLine: { show: false },
        axisLabel: { color: th.muted, fontSize: 10, hideOverlap: true, formatter: (v: number) => fmtTime(v) },
      },
      yAxis: yCategories
        ? {
            type: "category",
            data: yCategories,
            axisLine: { show: false },
            axisTick: { show: false },
            splitLine: { show: true, lineStyle: { color: th.grid } },
            axisLabel: { color: th.muted, fontSize: 10, width: 150, overflow: "truncate" },
          }
        : {
            type: "value",
            min: yMin,
            max: yMax,
            scale: yMin === undefined,
            splitNumber: compact ? 3 : 4,
            axisLine: { show: false },
            splitLine: { lineStyle: { color: th.grid } },
            axisLabel: { color: th.muted, fontSize: 10, formatter: (v: number) => format(v) },
          },
      series: s,
    };
  }, [series, x, th, alertMs, faultMs, legend, yMin, yMax, trigger, compact, format, maxTooltipRows, yCategories]);

  const onInit = useCallback(
    (chart: echarts.ECharts) => {
      const zr = chart.getZr();
      const hide = () => chart.setOption({ graphic: [{ id: "xh", type: "line", invisible: true, shape: { x1: 0, y1: 0, x2: 0, y2: 0 } }] }, { lazyUpdate: true });
      const onMove = (e: { offsetX: number; offsetY: number }) => {
        const p = [e.offsetX, e.offsetY];
        if (chart.containPixel("grid", p)) {
          const v = chart.convertFromPixel({ gridIndex: 0 }, p) as number[];
          hoverBus.publish(v[0], chart);
        } else hoverBus.publish(null, chart);
      };
      const onOut = () => hoverBus.publish(null, chart);
      zr.on("mousemove", onMove);
      zr.on("globalout", onOut);
      const unsub = hoverBus.subscribe((t, src) => {
        if (src === chart || chart.isDisposed()) return;
        if (t === null) return hide();
        const rect = gridRect(chart);
        if (!rect) return;
        const px = chart.convertToPixel({ gridIndex: 0 }, [t, 0]) as number[] | undefined;
        if (!px || px[0] < rect.x || px[0] > rect.x + rect.width) return hide();
        chart.setOption(
          {
            graphic: [
              {
                id: "xh",
                type: "line",
                invisible: false,
                silent: true,
                z: 50,
                shape: { x1: px[0], y1: rect.y, x2: px[0], y2: rect.y + rect.height },
                style: { stroke: CHART_THEMES[document.documentElement.classList.contains("dark") ? "dark" : "light"].crosshair, lineWidth: 1, lineDash: [3, 3] },
              },
            ],
          },
          { lazyUpdate: true },
        );
      });
      return () => {
        unsub();
        zr.off("mousemove", onMove);
        zr.off("globalout", onOut);
      };
    },
    [],
  );

  const hasData = series.some((s) => s.data.some((p) => p[1] !== null));
  return (
    <div className={`relative h-full w-full ${className ?? ""}`}>
      <EChart option={option} onInit={onInit} />
      {!hasData && (
        <div className="pointer-events-none absolute inset-0">
          <Empty title={emptyText} />
        </div>
      )}
    </div>
  );
}
