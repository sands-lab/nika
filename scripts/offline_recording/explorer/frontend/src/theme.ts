export type ThemeName = "dark" | "light";

export interface RoleStyle {
  color: string;
  label: string;
  icon: string; // SVG inner markup on a 24x24 grid, stroked
  shape: "ellipse" | "round-rectangle" | "round-hexagon" | "round-diamond" | "round-tag" | "barrel";
}

const ICONS = {
  router:
    '<circle cx="12" cy="12" r="8.5"/><path d="M12 5.5v5M12 13.5v5M5.5 12h5M13.5 12h5"/><path d="M10 7.5l2-2 2 2M10 16.5l2 2 2-2M7.5 10l-2 2 2 2M16.5 10l2 2-2 2"/>',
  switch:
    '<rect x="3" y="6" width="18" height="12" rx="2.5"/><path d="M7 10h9l-2-2M17 14H8l2 2"/>',
  host: '<rect x="3.5" y="4.5" width="17" height="11" rx="1.5"/><path d="M9 19.5h6M12 15.5v4"/>',
  service:
    '<rect x="4" y="3.5" width="16" height="7" rx="1.5"/><rect x="4" y="13.5" width="16" height="7" rx="1.5"/><path d="M8 7h.01M8 17h.01"/>',
  controller:
    '<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9.5" y="9.5" width="5" height="5" rx="1"/><path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3"/>',
  infrastructure:
    '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M4 7.5l8 4.5 8-4.5M12 12v9"/>',
  unknown: '<circle cx="12" cy="12" r="8"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .8-1 1.5v.4M12 17h.01"/>',
  hub: '<circle cx="12" cy="12" r="3"/>',
};

export const ROLE_STYLES: Record<string, RoleStyle> = {
  router: { color: "#5b8def", label: "Router", icon: ICONS.router, shape: "ellipse" },
  switch: { color: "#22c3dd", label: "Switch", icon: ICONS.switch, shape: "round-rectangle" },
  host: { color: "#a78bfa", label: "Host", icon: ICONS.host, shape: "round-rectangle" },
  service: { color: "#3fca7c", label: "Service", icon: ICONS.service, shape: "round-rectangle" },
  controller: { color: "#ec6fb4", label: "Controller", icon: ICONS.controller, shape: "round-hexagon" },
  infrastructure: { color: "#8d9bb5", label: "Infrastructure", icon: ICONS.infrastructure, shape: "round-rectangle" },
};

export const UNKNOWN_ROLE: RoleStyle = { color: "#6b7280", label: "Other", icon: ICONS.unknown, shape: "ellipse" };

export function roleStyle(role: string | undefined): RoleStyle {
  return (role && ROLE_STYLES[role]) || UNKNOWN_ROLE;
}

export function iconDataUri(inner: string, stroke = "#ffffff"): string {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="${stroke}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${inner}</svg>`;
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
}

export const ALERT_COLOR = "#f5a524";
export const FAULT_COLOR = "#f04452";
export const SNAPSHOT_COLOR = "#7c8db5";

export const SERIES_PALETTE = [
  "#5b8def",
  "#3fca7c",
  "#a78bfa",
  "#22c3dd",
  "#ec6fb4",
  "#e3c04b",
  "#f08a4b",
  "#2fb5a3",
  "#c77dde",
  "#8d9bb5",
  "#7fb5ff",
  "#b5d65a",
];

export interface ChartTheme {
  text: string;
  muted: string;
  axis: string;
  grid: string;
  tooltipBg: string;
  tooltipBorder: string;
  tooltipText: string;
  crosshair: string;
  bg: string;
}

export const CHART_THEMES: Record<ThemeName, ChartTheme> = {
  dark: {
    text: "#c9d1e0",
    muted: "#7f8aa3",
    axis: "#2d3546",
    grid: "#1f2633",
    tooltipBg: "rgba(17,21,30,0.96)",
    tooltipBorder: "#2f3850",
    tooltipText: "#dbe2ef",
    crosshair: "#56617a",
    bg: "transparent",
  },
  light: {
    text: "#334155",
    muted: "#64748b",
    axis: "#cbd5e1",
    grid: "#eef1f6",
    tooltipBg: "rgba(255,255,255,0.97)",
    tooltipBorder: "#d7dde8",
    tooltipText: "#1e293b",
    crosshair: "#94a3b8",
    bg: "transparent",
  },
};

export const GRAPH_THEMES: Record<ThemeName, { label: string; labelBg: string; edge: string; edgeHover: string; hub: string; selection: string; dim: number }> = {
  dark: { label: "#c3cbdb", labelBg: "#0d1117", edge: "#3a4459", edgeHover: "#8fa2c7", hub: "#4b566e", selection: "#e6edf7", dim: 0.18 },
  light: { label: "#334155", labelBg: "#ffffff", edge: "#b8c2d3", edgeHover: "#4f5f7d", hub: "#94a3b8", selection: "#0f172a", dim: 0.2 },
};
