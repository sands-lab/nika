const pad = (n: number, w = 2) => String(n).padStart(w, "0");

/** HH:MM:SS in UTC (the dataset is recorded in UTC). */
export function fmtTime(ms: number, withMs = false): string {
  const d = new Date(ms);
  const s = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
  return withMs ? `${s}.${pad(d.getUTCMilliseconds(), 3)}` : s;
}

export function fmtDateTime(ms: number): string {
  const d = new Date(ms);
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${fmtTime(ms)} UTC`;
}

/** Signed offset like "+1:23" or "−0:05" relative to a reference. */
export function fmtOffset(ms: number, ref: number): string {
  const d = Math.round((ms - ref) / 1000);
  const sign = d < 0 ? "−" : "+";
  const a = Math.abs(d);
  return `${sign}${Math.floor(a / 60)}:${pad(a % 60)}`;
}

export function fmtDuration(ms: number): string {
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return `${m}m${s % 60 ? ` ${s % 60}s` : ""}`;
}

const SI = ["", "k", "M", "G", "T", "P"];

export function fmtSI(v: number | null | undefined, unit = "", digits = 3): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  const a = Math.abs(v);
  if (a !== 0 && a < 0.01) return `${v.toExponential(1)}${unit ? ` ${unit}` : ""}`;
  let i = 0;
  let x = v;
  while (Math.abs(x) >= 1000 && i < SI.length - 1) {
    x /= 1000;
    i++;
  }
  const str = Number.isInteger(x) ? String(x) : x.toPrecision(digits).replace(/\.?0+$/, "");
  return `${str}${SI[i] || unit ? " " : ""}${SI[i]}${unit}`;
}

export function fmtBytes(v: number | null | undefined): string {
  if (v === null || v === undefined) return "–";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0;
  let x = v;
  while (Math.abs(x) >= 1024 && i < units.length - 1) {
    x /= 1024;
    i++;
  }
  return `${i === 0 ? x : x.toFixed(1)} ${units[i]}`;
}

export function fmtInt(v: number | null | undefined): string {
  if (v === null || v === undefined) return "–";
  return v.toLocaleString("en-US");
}

/** Guesses a display unit for a Prometheus metric name. */
export function metricUnit(metric: string, rate: boolean): { unit: string; scale: number } {
  const m = metric.toLowerCase();
  if (/(bytes|octets)/.test(m)) return rate ? { unit: "bit/s", scale: 8 } : { unit: "B", scale: 1 };
  if (/seconds/.test(m)) return rate ? { unit: "s/s", scale: 1 } : { unit: "s", scale: 1 };
  if (/packets/.test(m)) return rate ? { unit: "pkt/s", scale: 1 } : { unit: "pkt", scale: 1 };
  return rate ? { unit: "/s", scale: 1 } : { unit: "", scale: 1 };
}

export function naturalCompare(a: string, b: string): number {
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
}
