import { useEffect, useMemo, useRef, useState } from "react";
import cytoscape, { type Core, type ElementDefinition, type StylesheetJson } from "cytoscape";
import { parseComponent, useStore } from "../store";
import { computeLayout, hubId } from "../layout";
import { FAULT_COLOR, GRAPH_THEMES, ROLE_STYLES, UNKNOWN_ROLE, iconDataUri, roleStyle, type ThemeName } from "../theme";
import type { TaskDetail } from "../types";
import { IconFit } from "./ui/icons";
import { cx } from "./ui/primitives";

function buildElements(task: TaskDetail): { els: ElementDefinition[]; mesh: boolean } {
  const { positions: pos, mesh: isMesh, sideHubs } = computeLayout(task.devices, task.segments);
  const els: ElementDefinition[] = [];
  const known = new Set(task.devices.map((d) => d.name));
  for (const d of task.devices) {
    const rs = roleStyle(d.role);
    els.push({
      group: "nodes",
      data: { id: d.name, label: d.name, role: d.role, color: rs.color, icon: iconDataUri(rs.icon), shape: rs.shape, unknownRole: !ROLE_STYLES[d.role] },
      position: pos.get(d.name) ?? { x: 0, y: 0 },
    });
  }
  for (const s of task.segments) {
    const ends = s.ends.filter((e) => known.has(e.device));
    if (s.ends.length > 2) {
      const h = hubId(s);
      const mgmt = sideHubs.has(h) ? " mgmt" : "";
      els.push({ group: "nodes", data: { id: h, seg: s.id, hub: true, label: "" }, position: pos.get(h) ?? { x: 0, y: 0 }, classes: `hub${mgmt}` });
      for (const e of ends) {
        els.push({ group: "edges", data: { id: `${s.id}|${e.device}|${e.port}`, source: h, target: e.device, seg: s.id, tgtPort: e.port, srcPort: "", ports: `${e.device}:${e.port}` }, classes: `hubedge${mgmt}` });
      }
    } else if (ends.length === 2) {
      els.push({
        group: "edges",
        data: { id: s.id, source: ends[0].device, target: ends[1].device, seg: s.id, srcPort: ends[0].port, tgtPort: ends[1].port, ports: `${ends[0].device}:${ends[0].port} ↔ ${ends[1].device}:${ends[1].port}` },
      });
    }
  }
  return { els, mesh: isMesh };
}

function buildStyle(theme: ThemeName): StylesheetJson {
  const g = GRAPH_THEMES[theme];
  return [
    {
      selector: "node",
      style: {
        width: 38,
        height: 38,
        shape: "data(shape)" as never,
        "background-color": "data(color)",
        "background-image": "data(icon)",
        "background-width": "62%",
        "background-height": "62%",
        "background-clip": "none",
        "border-width": 0,
        label: "data(label)",
        "font-family": "Inter Variable, system-ui, sans-serif",
        "font-size": 11.5,
        "font-weight": 500,
        color: g.label,
        "text-valign": "bottom",
        "text-margin-y": 5,
        "text-background-color": g.labelBg,
        "text-background-opacity": 0.8,
        "text-background-padding": "1.5px",
        "text-background-shape": "roundrectangle",
        "overlay-opacity": 0,
        "transition-property": "opacity, border-width, width, height",
        "transition-duration": 120,
      },
    },
    { selector: "node[?unknownRole]", style: { "border-width": 1.5, "border-style": "dashed", "border-color": g.label } },
    {
      selector: "node.hub",
      style: { width: 14, height: 14, shape: "ellipse", "background-color": g.hub, "background-image": "none", label: "", "border-width": 2, "border-color": g.labelBg },
    },
    {
      selector: "edge",
      style: {
        width: 1.6,
        "line-color": g.edge,
        "curve-style": "bezier",
        "overlay-opacity": 0,
        "font-family": "JetBrains Mono Variable, monospace",
        "font-size": 10,
        color: g.label,
        "text-background-color": g.labelBg,
        "text-background-opacity": 0.9,
        "text-background-padding": "1px",
        "source-text-offset": 20,
        "target-text-offset": 20,
      },
    },
    { selector: "edge.hubedge", style: { width: 1.3, "line-style": "dashed", "line-dash-pattern": [4, 3] } },
    { selector: "edge.hubedge.mgmt", style: { width: 1, opacity: 0.45 } },
    { selector: "edge.hover, edge.ports", style: { opacity: 1, "line-color": g.edgeHover, width: 2.4, "source-label": "data(srcPort)", "target-label": "data(tgtPort)", "z-index": 10 } },
    { selector: "node.hub.hover", style: { "background-color": g.edgeHover } },
    { selector: "node.hover", style: { "border-width": 2, "border-color": g.edgeHover, "border-style": "solid" } },
    { selector: "node.sel", style: { "border-width": 3, "border-color": g.selection, "border-style": "solid", width: 44, height: 44, "font-weight": 700, "z-index": 20 } },
    { selector: "node.hub.sel", style: { width: 18, height: 18, "background-color": g.selection, "border-width": 2, "border-color": g.labelBg } },
    { selector: "edge.sel", style: { opacity: 1, "line-color": g.selection, width: 3, "source-label": "data(srcPort)", "target-label": "data(tgtPort)", "z-index": 15 } },
    { selector: "node.faulty", style: { "border-width": 4, "border-color": FAULT_COLOR, "border-style": "double", "underlay-color": FAULT_COLOR, "underlay-opacity": 0.28, "underlay-padding": 7, "underlay-shape": "ellipse", color: FAULT_COLOR, "font-weight": 700 } },
    { selector: "edge.faulty", style: { "line-color": FAULT_COLOR, width: 3, "source-label": "data(srcPort)", "target-label": "data(tgtPort)", color: FAULT_COLOR } },
    { selector: ".dim", style: { opacity: g.dim } },
  ];
}

export function TopologyGraph() {
  const { task, theme, select, selection, label, revealed } = useStore();
  const ref = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const [hoverInfo, setHoverInfo] = useState<string | null>(null);
  const userMoved = useRef(false);
  const selectRef = useRef(select);
  selectRef.current = select;

  // (Re)create the graph for each task.
  useEffect(() => {
    if (!ref.current || !task) return;
    const { els, mesh } = buildElements(task);
    const cy = cytoscape({
      container: ref.current,
      elements: els,
      style: buildStyle(theme),
      layout: { name: "preset", fit: true, padding: 36 },
      minZoom: 0.2,
      maxZoom: 3.5,
      boxSelectionEnabled: false,
      autoungrabify: false,
    });
    cyRef.current = cy;
    if (mesh) {
      // Meshes (e.g. ISP backbones) read better force-directed; start from the layered positions.
      cy.layout({ name: "cose", animate: false, randomize: false, fit: false, nodeRepulsion: () => 9000, idealEdgeLength: () => 70, nodeOverlap: 12, numIter: 1500 } as cytoscape.LayoutOptions).run();
    }
    if (import.meta.env.DEV) (window as unknown as { __cy?: Core }).__cy = cy;
    cy.on("tap", "node", (e) => {
      const n = e.target;
      if (n.data("hub")) selectRef.current({ kind: "segment", id: n.data("seg") });
      else selectRef.current({ kind: "device", name: n.id() });
    });
    cy.on("tap", "edge", (e) => selectRef.current({ kind: "segment", id: e.target.data("seg") }));
    cy.on("tap", (e) => {
      if (e.target === cy) selectRef.current(null);
    });
    cy.on("mouseover", "edge", (e) => {
      const seg = e.target.data("seg");
      cy.edges().filter((x) => x.data("seg") === seg).addClass("hover");
      cy.nodes().filter((x) => x.data("seg") === seg).addClass("hover");
      setHoverInfo(e.target.data("ports"));
      if (ref.current) ref.current.style.cursor = "pointer";
    });
    cy.on("mouseout", "edge", () => {
      cy.elements().removeClass("hover");
      setHoverInfo(null);
      if (ref.current) ref.current.style.cursor = "";
    });
    cy.on("mouseover", "node", (e) => {
      const n = e.target;
      n.addClass("hover");
      if (n.data("hub")) {
        n.connectedEdges().addClass("hover");
        setHoverInfo(`LAN ${n.data("seg")}: ${n.connectedEdges().map((x: cytoscape.EdgeSingular) => x.data("ports")).join(", ")}`);
      } else {
        n.connectedEdges().addClass("ports");
        setHoverInfo(`${n.id()} · ${n.data("role")}`);
      }
      if (ref.current) ref.current.style.cursor = "pointer";
    });
    cy.on("mouseout", "node", (e) => {
      e.target.removeClass("hover");
      cy.elements().removeClass("hover");
      cy.edges().removeClass("ports");
      setHoverInfo(null);
      if (ref.current) ref.current.style.cursor = "";
    });
    // Keep the graph fitted to its container until the user pans or zooms.
    const el = ref.current;
    const markUser = () => (userMoved.current = true);
    el.addEventListener("wheel", markUser, { passive: true });
    let down = false;
    const onDown = () => (down = true);
    const onUp = () => (down = false);
    el.addEventListener("pointerdown", onDown);
    window.addEventListener("pointerup", onUp);
    cy.on("pan", () => {
      if (down) markUser();
    });
    userMoved.current = false;
    const refit = () => {
      cy.resize();
      if (!userMoved.current && el.clientWidth > 0 && el.clientHeight > 0) cy.fit(undefined, 36);
    };
    const ro = new ResizeObserver(refit);
    ro.observe(el);
    const raf = requestAnimationFrame(refit);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      el.removeEventListener("wheel", markUser);
      el.removeEventListener("pointerdown", onDown);
      window.removeEventListener("pointerup", onUp);
      cy.destroy();
      cyRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [task]);

  // Theme
  useEffect(() => {
    cyRef.current?.style(buildStyle(theme));
  }, [theme]);

  // Selection highlighting
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.elements().removeClass("sel dim");
    if (!selection) return;
    if (selection.kind === "device") {
      const n = cy.getElementById(selection.name);
      if (n.nonempty()) {
        n.addClass("sel");
        n.connectedEdges().addClass("ports");
        const nb = n.closedNeighborhood();
        cy.elements().not(nb).addClass("dim");
      }
    } else {
      const els = cy.elements().filter((x) => x.data("seg") === selection.id);
      els.addClass("sel");
      const nb = els.union(els.connectedNodes());
      cy.elements().not(nb).addClass("dim");
    }
  }, [selection, task]);

  // Fault highlighting
  const faulty = useMemo(() => {
    if (!revealed || !label) return [];
    return label.root_causes.flatMap((rc) => rc.component.split(";").map((c) => parseComponent(c)).filter((x): x is NonNullable<typeof x> => !!x));
  }, [label, revealed]);
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.elements().removeClass("faulty");
    for (const f of faulty) {
      cy.getElementById(f.device).addClass("faulty");
      if (f.port) {
        cy.edges()
          .filter((e) => (e.data("source") === f.device && e.data("srcPort") === f.port) || (e.data("target") === f.device && e.data("tgtPort") === f.port))
          .addClass("faulty");
      }
    }
  }, [faulty, task]);

  const roles = useMemo(() => {
    if (!task) return [];
    const present = [...new Set(task.devices.map((d) => d.role))];
    return present
      .map((r) => ({ role: r, style: ROLE_STYLES[r] ?? UNKNOWN_ROLE }))
      .sort((a, b) => Object.keys(ROLE_STYLES).indexOf(a.role) - Object.keys(ROLE_STYLES).indexOf(b.role));
  }, [task]);
  const hasLan = !!task?.segments.some((s) => s.ends.length > 2);

  return (
    <div className="relative h-full w-full overflow-hidden">
      <div ref={ref} className="h-full w-full" />
      {/* legend */}
      <div className="pointer-events-none absolute bottom-2 left-2 flex max-w-[70%] flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-line bg-surface/85 px-2.5 py-1.5 backdrop-blur-sm">
        {roles.map(({ role, style }) => (
          <span key={role} className="flex items-center gap-1.5 text-[10px] text-fg-2">
            <span className={cx("inline-flex h-3.5 w-3.5 items-center justify-center", style.shape === "ellipse" ? "rounded-full" : "rounded-[4px]")} style={{ background: style.color }}>
              <img src={iconDataUri(style.icon)} alt="" className="h-2.5 w-2.5" />
            </span>
            {ROLE_STYLES[role] ? style.label : role}
          </span>
        ))}
        {hasLan && (
          <span className="flex items-center gap-1.5 text-[10px] text-fg-2">
            <span className="inline-block h-2.5 w-2.5 rounded-full bg-fg-3" />
            LAN segment
          </span>
        )}
        {faulty.length > 0 && (
          <span className="flex items-center gap-1.5 text-[10px] font-medium text-fault">
            <span className="inline-block h-2.5 w-2.5 rounded-full border-2 border-fault" />
            faulty component
          </span>
        )}
      </div>
      {/* controls */}
      <div className="absolute right-2 top-2 flex flex-col gap-1">
        <button
          title="Fit to view"
          onClick={() => {
            userMoved.current = false;
            cyRef.current?.animate({ fit: { eles: cyRef.current.elements(), padding: 36 } }, { duration: 200 });
          }}
          className="flex h-7 w-7 items-center justify-center rounded-md border border-line bg-surface/90 text-fg-2 hover:text-fg"
        >
          <IconFit size={14} />
        </button>
        <button title="Zoom in" onClick={() => zoomBy(cyRef.current, 1.25)} className="flex h-7 w-7 items-center justify-center rounded-md border border-line bg-surface/90 text-sm text-fg-2 hover:text-fg">
          +
        </button>
        <button title="Zoom out" onClick={() => zoomBy(cyRef.current, 0.8)} className="flex h-7 w-7 items-center justify-center rounded-md border border-line bg-surface/90 text-sm text-fg-2 hover:text-fg">
          −
        </button>
      </div>
      {hoverInfo && (
        <div className="pointer-events-none absolute left-2 top-2 max-w-[80%] truncate rounded-md border border-line bg-surface/90 px-2 py-1 font-mono text-[10.5px] text-fg-2">{hoverInfo}</div>
      )}
      {!selection && task && (
        <div className="pointer-events-none absolute bottom-2 right-2 text-[10px] text-fg-3">Click a device, link or LAN hub to inspect it</div>
      )}
    </div>
  );
}

function zoomBy(cy: Core | null, f: number) {
  if (!cy) return;
  const z = cy.zoom() * f;
  cy.zoom({ level: z, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
}
