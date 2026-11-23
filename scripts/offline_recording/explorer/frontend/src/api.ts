import type {
  ControllerFile,
  HttpDnsRow,
  IntRecords,
  IntSummary,
  InterfaceData,
  IperfRow,
  Label,
  MetricData,
  MetricInfo,
  NetflowData,
  PingmeshData,
  SyslogData,
  TaskDetail,
  TaskSummary,
  TracerouteEntry,
  ViewText,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

type Params = Record<string, string | number | boolean | undefined | null | (string | number)[]>;

function buildUrl(path: string, params?: Params): string {
  const qs = new URLSearchParams();
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === null || v === "") continue;
      if (Array.isArray(v)) v.forEach((x) => qs.append(k, String(x)));
      else qs.append(k, String(v));
    }
  }
  const q = qs.toString();
  return `api${path}${q ? `?${q}` : ""}`;
}

// Small LRU-ish cache of in-flight and settled requests, keyed by URL.
const cache = new Map<string, Promise<unknown>>();
const CACHE_MAX = 400;

async function fetchJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const body = await res.json();
      msg = typeof body?.detail === "string" ? body.detail : JSON.stringify(body?.detail ?? body);
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, `${res.status} ${msg}`);
  }
  return (await res.json()) as T;
}

export function get<T>(path: string, params?: Params, useCache = true): Promise<T> {
  const url = buildUrl(path, params);
  if (useCache) {
    const hit = cache.get(url);
    if (hit) {
      cache.delete(url);
      cache.set(url, hit);
      return hit as Promise<T>;
    }
  }
  const p = fetchJson<T>(url);
  if (useCache) {
    cache.set(url, p);
    p.catch(() => cache.delete(url));
    if (cache.size > CACHE_MAX) {
      const first = cache.keys().next().value;
      if (first !== undefined) cache.delete(first);
    }
  }
  return p;
}

const t = (tid: string) => `/tasks/${encodeURIComponent(tid)}`;

export const topologyPngUrl = (tid: string) => buildUrl(`${t(tid)}/topology.png`);

export const api = {
  tasks: () => get<TaskSummary[]>("/tasks"),
  task: (tid: string) => get<TaskDetail>(t(tid)),
  label: (tid: string) => get<Label>(`${t(tid)}/label`),
  view: (tid: string, dev: string, view: string, phase: "snapshot" | "before_fault" = "snapshot") =>
    get<ViewText>(`${t(tid)}/devices/${encodeURIComponent(dev)}/views/${encodeURIComponent(view)}`, { phase }),
  controller: (tid: string, name: string) => get<ControllerFile>(`${t(tid)}/controllers/${encodeURIComponent(name)}`),
  metrics: (tid: string) => get<MetricInfo[]>(`${t(tid)}/metrics`),
  metric: (tid: string, metric: string, devices?: string[], rate: "auto" | "true" | "false" = "auto") =>
    get<MetricData>(`${t(tid)}/metrics/${encodeURIComponent(metric)}`, { device: devices, rate }),
  iface: (tid: string, device: string, port: string) => get<InterfaceData>(`${t(tid)}/interface`, { device, port }),
  syslog: (tid: string, p: { device?: string[]; program?: string[]; q?: string; limit?: number; start?: number; end?: number }) =>
    get<SyslogData>(`${t(tid)}/syslog`, p),
  netflow: (tid: string, p: { exporter?: string; q?: string; limit?: number; start?: number; end?: number }) =>
    get<NetflowData>(`${t(tid)}/netflow`, p),
  pingmesh: (tid: string) => get<PingmeshData>(`${t(tid)}/probes/pingmesh`),
  httpDns: (tid: string) => get<{ rows: HttpDnsRow[] }>(`${t(tid)}/probes/http_dns`),
  iperf: (tid: string) => get<{ rows: IperfRow[] }>(`${t(tid)}/probes/iperf`),
  traceroute: (tid: string) => get<TracerouteEntry[]>(`${t(tid)}/probes/traceroute`),
  intSummary: (tid: string) => get<IntSummary>(`${t(tid)}/int/summary`),
  intRecords: (tid: string, offset: number, limit: number) => get<IntRecords>(`${t(tid)}/int/records`, { offset, limit }),
};
