// Generic topology layout, decided from graph structure only (never from network names).
// It follows the same rules as the pre-rendered diagram (offline_recording/topology/draw_topology.py):
//
// * Top tier: per connected component, the forwarding devices whose names carry the
//   highest-ranked tier keyword (core/gateway/border > spine/aggregation > distribution >
//   leaf/access/edge); without keywords, the forwarding devices farthest from end hosts.
// * Every other device sits at its BFS depth below the top tier; end hosts attached
//   directly to the top tier are drawn above it. Row order is refined with barycenter sweeps.
// * Meshes (most forwarding-to-forwarding links inside one tier) are flagged so the caller
//   can run a force-directed layout instead.
// * A shared LAN whose members also have other links (an out-of-band control/telemetry
//   network) does not take part in the layering; its hub is placed to the side, together
//   with devices that only hang off it.
import type { Segment, Device } from "./types";
import { naturalCompare } from "./format";

export interface LayoutResult {
  positions: Map<string, { x: number; y: number }>;
  mesh: boolean;
  sideHubs: Set<string>;
}

const TIER_KEYWORDS: Record<string, number> = {
  core: 0,
  gateway: 0,
  gw: 0,
  border: 0,
  spine: 1,
  agg: 1,
  aggregation: 1,
  dist: 2,
  distribution: 2,
  leaf: 3,
  access: 3,
  tor: 3,
  edge: 3,
};

function keywordRank(name: string): number | null {
  const ranks = name
    .toLowerCase()
    .split(/[_\-\d]+/)
    .filter((t) => t in TIER_KEYWORDS)
    .map((t) => TIER_KEYWORDS[t]);
  return ranks.length ? Math.min(...ranks) : null;
}

export function hubId(seg: Segment): string {
  return `hub:${seg.id}`;
}

function bfs(adj: Map<string, Set<string>>, sources: string[], within?: Set<string>): Map<string, number> {
  const d = new Map<string, number>();
  const q = [...sources];
  sources.forEach((s) => d.set(s, 0));
  while (q.length) {
    const u = q.shift()!;
    for (const v of adj.get(u) ?? []) {
      if (d.has(v) || (within && !within.has(v))) continue;
      d.set(v, d.get(u)! + 1);
      q.push(v);
    }
  }
  return d;
}

export function computeLayout(devices: Device[], segments: Segment[]): LayoutResult {
  const role = new Map(devices.map((d) => [d.name, d.role]));
  const nodes: string[] = devices.map((d) => d.name);
  const adj = new Map<string, Set<string>>();
  nodes.forEach((n) => adj.set(n, new Set()));
  const link = (a: string, b: string) => {
    adj.get(a)!.add(b);
    adj.get(b)!.add(a);
  };

  // Count each device's point-to-point links to spot out-of-band LANs.
  const p2pDegree = new Map<string, number>();
  for (const s of segments) {
    const ends = [...new Set(s.ends.map((e) => e.device))].filter((d) => role.has(d));
    if (s.ends.length <= 2 && ends.length === 2) ends.forEach((d) => p2pDegree.set(d, (p2pDegree.get(d) ?? 0) + 1));
  }
  const sideHubs = new Set<string>();
  const sideMembers = new Map<string, string[]>();
  for (const s of segments) {
    const ends = [...new Set(s.ends.map((e) => e.device))].filter((d) => role.has(d));
    if (s.ends.length > 2) {
      const h = hubId(s);
      nodes.push(h);
      adj.set(h, new Set());
      const withOther = ends.filter((d) => (p2pDegree.get(d) ?? 0) > 0);
      if (withOther.length >= 2) {
        sideHubs.add(h);
        sideMembers.set(h, ends);
      } else ends.forEach((d) => link(h, d));
    } else if (ends.length === 2) link(ends[0], ends[1]);
  }

  const deg = (n: string) => adj.get(n)?.size ?? 0;
  const isFwd = (n: string) => ["router", "switch"].includes(role.get(n) ?? "") || (!n.startsWith("hub:") && deg(n) >= 2);

  // --- tiers per connected component (side hubs excluded)
  const depth = new Map<string, number>();
  const layoutNodes = nodes.filter((n) => !sideHubs.has(n) && deg(n) > 0);
  const seen = new Set<string>();
  for (const start of layoutNodes) {
    if (seen.has(start)) continue;
    const comp = new Set(bfs(adj, [start]).keys());
    comp.forEach((n) => seen.add(n));
    let cf = [...comp].filter((n) => isFwd(n) && !n.startsWith("hub:"));
    if (!cf.length) cf = [...comp].filter((n) => !n.startsWith("hub:")).sort(naturalCompare);
    const ranked = cf.map((n) => ({ n, r: keywordRank(n) })).filter((x) => x.r !== null) as { n: string; r: number }[];
    let top: string[];
    if (ranked.length) {
      const best = Math.min(...ranked.map((x) => x.r));
      top = ranked.filter((x) => x.r === best).map((x) => x.n);
    } else {
      const ends = [...comp].filter((n) => deg(n) <= 1);
      const height = bfs(adj, ends.length ? ends : cf, comp);
      const hmax = Math.max(...cf.map((n) => height.get(n) ?? 0));
      top = cf.filter((n) => (height.get(n) ?? 0) === hmax);
    }
    const d = bfs(adj, top, comp);
    for (const n of comp) {
      if (deg(n) === 1 && !isFwd(n)) {
        const nb = [...adj.get(n)!][0];
        if (d.get(nb) === 0) d.set(n, -1);
      }
    }
    d.forEach((v, k) => depth.set(k, v));
  }

  // --- mesh detection
  const ff: [string, string][] = [];
  for (const a of layoutNodes) for (const b of adj.get(a) ?? []) if (a < b && isFwd(a) && isFwd(b) && !a.startsWith("hub:") && !b.startsWith("hub:")) ff.push([a, b]);
  const mesh = ff.length >= 4 && ff.filter(([a, b]) => depth.get(a) === depth.get(b)).length / ff.length > 0.5;

  // --- rows
  const minD = Math.min(0, ...depth.values());
  const maxD = Math.max(0, ...depth.values());
  const rows: string[][] = [];
  for (let r = minD; r <= maxD; r++) rows.push(layoutNodes.filter((n) => depth.get(n) === r));
  const isolated = nodes.filter((n) => !sideHubs.has(n) && !depth.has(n) && ![...sideMembers.values()].some((m) => m.includes(n)));
  if (isolated.length) rows.push(isolated);
  const nonEmpty = rows.filter((r) => r.length);
  const rowIndex = new Map<string, number>();
  nonEmpty.forEach((r, i) => r.forEach((n) => rowIndex.set(n, i)));

  // --- barycenter ordering
  const order = new Map<string, number>();
  const firstNb = (n: string) => [...(adj.get(n) ?? [])].filter((v) => (rowIndex.get(v) ?? 99) < (rowIndex.get(n) ?? 0)).sort(naturalCompare)[0] ?? n;
  nonEmpty.forEach((r) => {
    r.sort((a, b) => naturalCompare(firstNb(a), firstNb(b)) || naturalCompare(a, b));
    r.forEach((n, i) => order.set(n, i / Math.max(1, r.length - 1)));
  });
  const sweep = (idxs: number[]) => {
    for (const i of idxs) {
      const r = nonEmpty[i];
      const bary = new Map<string, number>();
      for (const n of r) {
        const ns = [...(adj.get(n) ?? [])].filter((v) => rowIndex.has(v) && rowIndex.get(v) !== i);
        bary.set(n, ns.length ? ns.reduce((s, v) => s + order.get(v)!, 0) / ns.length : order.get(n)!);
      }
      r.sort((a, b) => bary.get(a)! - bary.get(b)! || order.get(a)! - order.get(b)!);
      r.forEach((n, k) => order.set(n, k / Math.max(1, r.length - 1)));
    }
  };
  const idx = nonEmpty.map((_, i) => i);
  for (let k = 0; k < 4; k++) {
    sweep(idx);
    sweep([...idx].reverse());
  }

  // --- coordinates
  const GAP_X = 112;
  const GAP_Y = nonEmpty.length > 5 ? Math.max(72, Math.round((104 * 5) / nonEmpty.length)) : 104;
  const pos = new Map<string, { x: number; y: number }>();
  const place = (r: string[], desired: Map<string, number>) => {
    const xs = r.map((n) => desired.get(n)!);
    for (let k = 1; k < xs.length; k++) xs[k] = Math.max(xs[k], xs[k - 1] + GAP_X);
    const shift = r.reduce((s, n, k) => s + (desired.get(n)! - xs[k]), 0) / Math.max(1, r.length);
    return xs.map((x) => x + shift);
  };
  const anchor = nonEmpty.reduce((best, r, i) => (r.length > nonEmpty[best].length ? i : best), 0);
  const visit = [anchor, ...idx.filter((i) => i > anchor), ...idx.filter((i) => i < anchor).reverse()];
  const placed = new Set<string>();
  for (const i of visit) {
    const r = nonEmpty[i];
    const desired = new Map<string, number>();
    r.forEach((n, k) => {
      const ns = [...(adj.get(n) ?? [])].filter((v) => placed.has(v));
      desired.set(n, i !== anchor && ns.length ? ns.reduce((s, v) => s + pos.get(v)!.x, 0) / ns.length : (k - (r.length - 1) / 2) * GAP_X * 1.15);
    });
    place(r, desired).forEach((x, k) => {
      pos.set(r[k], { x, y: i * GAP_Y });
      placed.add(r[k]);
    });
  }
  // Pull rows toward all their neighbours once more (parents over children).
  for (const i of idx) {
    const r = nonEmpty[i];
    const desired = new Map<string, number>();
    r.forEach((n) => {
      const ns = [...(adj.get(n) ?? [])].filter((v) => pos.has(v));
      desired.set(n, ns.length ? ns.reduce((s, v) => s + pos.get(v)!.x, 0) / ns.length : pos.get(n)!.x);
    });
    place(r, desired).forEach((x, k) => (pos.get(r[k])!.x = x));
  }
  // Stagger very wide rows of end devices so labels stay legible.
  nonEmpty.forEach((r) => {
    if (r.length < 9) return;
    r.forEach((n, k) => {
      if (k % 2 === 1 && !isFwd(n)) pos.get(n)!.y += GAP_Y * 0.4;
    });
  });

  // --- side column for out-of-band LANs and devices that only hang off them
  const xs = [...pos.values()].map((p) => p.x);
  const ys = [...pos.values()].map((p) => p.y);
  const right = (xs.length ? Math.max(...xs) : 0) + GAP_X * 1.6;
  const midY = ys.length ? (Math.min(...ys) + Math.max(...ys)) / 2 : 0;
  const hubs = [...sideHubs].sort(naturalCompare);
  const side: string[] = [];
  hubs.forEach((h) => {
    side.push(h);
    for (const m of sideMembers.get(h) ?? []) if (!pos.has(m) && !side.includes(m)) side.push(m);
  });
  side.forEach((n, k) => {
    const isHub = sideHubs.has(n);
    pos.set(n, { x: right + (isHub ? 0 : GAP_X * 0.9), y: midY + (k - (side.length - 1) / 2) * GAP_Y * 0.8 });
  });
  return { positions: pos, mesh, sideHubs };
}
