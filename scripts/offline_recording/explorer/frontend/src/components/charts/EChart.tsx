import { useEffect, useRef } from "react";
import { echarts, type ECOption } from "./echarts";

/** Thin ECharts wrapper: owns the instance, resizes with its container, replaces options on change. */
export function EChart({
  option,
  className,
  style,
  onInit,
  onEvents,
}: {
  option: ECOption;
  className?: string;
  style?: React.CSSProperties;
  onInit?: (chart: echarts.ECharts) => (() => void) | void;
  onEvents?: Record<string, (params: unknown) => void>;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);
  const eventsRef = useRef(onEvents);
  eventsRef.current = onEvents;

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const chart = echarts.init(el, undefined, { renderer: "canvas" });
    chartRef.current = chart;
    const cleanup = onInit?.(chart);
    const ro = new ResizeObserver(() => {
      if (el.clientWidth > 0 && el.clientHeight > 0) chart.resize();
    });
    ro.observe(el);
    const names = Object.keys(eventsRef.current ?? {});
    names.forEach((n) => chart.on(n, (p: unknown) => eventsRef.current?.[n]?.(p)));
    return () => {
      ro.disconnect();
      cleanup?.();
      chart.dispose();
      chartRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    chartRef.current?.setOption(option, { notMerge: true, lazyUpdate: true });
  }, [option]);

  return <div ref={ref} className={className} style={{ width: "100%", height: "100%", ...style }} />;
}
