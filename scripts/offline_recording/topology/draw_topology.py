# /// script
# requires-python = ">=3.10"
# dependencies = ["matplotlib>=3.7", "networkx>=3.0", "numpy>=1.24", "scipy>=1.10"]
# ///
"""Draw a network topology diagram (PNG) for every task of a recorded dataset.

    uv run scripts/offline_recording/topology/draw_topology.py <dataset_dir>
    uv run scripts/offline_recording/topology/draw_topology.py --task <task_dir>

Reads ``tasks/<task_id>/task.json`` (fields ``network``, ``description``,
``devices`` and ``segments``) and writes ``tasks/<task_id>/topology.png``.
Labels are never read, so the figure does not reveal the fault.

Layout is chosen from the graph structure, not from the network name:

* Hierarchical networks get a layered (tiered) drawing. The top tier is the set
  of forwarding devices whose names carry the highest-ranked tier keyword
  (core/gateway/border > spine/aggregation > distribution > leaf/access/edge);
  without keywords it is the set of forwarding devices farthest from the
  end hosts. Every other device sits at its BFS depth below the top tier, and
  end hosts attached directly to the top tier are drawn above it. Tier order is
  refined with barycenter sweeps to reduce crossings.
* Meshes (most forwarding-to-forwarding links inside one tier, e.g. an ISP
  backbone) get a force-directed (Kamada-Kawai) layout of the forwarding
  devices, with each end host placed next to its router in the freest
  direction.
* A shared LAN segment (more than two ends) whose members have no other link,
  except one hub, is drawn as a bus below the hub. A shared LAN whose members
  also have other links (typically an out-of-band control or telemetry
  network) is drawn as a lettered bus on the right; members carry a matching
  "letter:port" tag so the fabric drawing stays readable.

Icons: original vector shapes drawn with matplotlib patches in this file,
following the conventional Cisco-style network diagram shapes (router =
circle with four arrows, switch = box with two opposing arrows, PC = monitor,
server = tower). No third-party artwork or icon files are used, so there is no
external license to track; the drawing code is covered by this repository's
license.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import networkx as nx  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import (  # noqa: E402
    Circle,
    FancyArrow,
    FancyBboxPatch,
    Polygon,
    Rectangle,
)
from matplotlib.path import Path as MPath  # noqa: E402
from matplotlib.patches import PathPatch  # noqa: E402

# ---------------------------------------------------------------- style ----

W, H = 1600, 900  # output size in pixels (figure is drawn in pixel units)
DPI = 100
TITLE_H = 92  # reserved band at the top
LEGEND_H = 58  # reserved band at the bottom
MARGIN_X = 40
SIDE_W = 250  # right-hand panel for out-of-band LANs
ICON = 40  # nominal icon size (px)

FONT = "DejaVu Sans"
NAME_PT = 10.5
CAPS_PT = 8.0
PORT_PT = 7.5
TAG_PT = 7.5

ROLE_STYLE = {
    "router": {"color": "#1f6fb2", "label": "Router"},
    "switch": {"color": "#22917f", "label": "Switch"},
    "host": {"color": "#4a5568", "label": "Host / PC"},
    "service": {"color": "#d9822b", "label": "Service (server)"},
    "controller": {"color": "#7b4fa6", "label": "Controller"},
    "infrastructure": {"color": "#5f7a8c", "label": "Infrastructure"},
    "unknown": {"color": "#8a8a8a", "label": "Other"},
}
ROLE_ORDER = list(ROLE_STYLE)
LINK_COLOR = "#5b6270"
LAN_COLORS = ["#c0392b", "#2c7a3f", "#8e44ad", "#b7791f", "#1f6f8b"]
PORT_COLOR = "#7a7f87"

# Generic tier vocabulary (lower rank = higher in the drawing).
TIER_KEYWORDS = {
    "core": 0,
    "gateway": 0,
    "gw": 0,
    "border": 0,
    "spine": 1,
    "agg": 1,
    "aggregation": 1,
    "dist": 2,
    "distribution": 2,
    "leaf": 3,
    "access": 3,
    "tor": 3,
    "edge": 3,
}

CAP_LABELS = {
    "frr": "FRR",
    "bgp": "BGP",
    "ospf": "OSPF",
    "isis": "IS-IS",
    "sonic": "SONiC",
    "static_routing": "static",
    "bmv2": "BMv2",
    "p4": "P4",
    "p4runtime": "P4Runtime",
    "ovs": "OVS",
    "openflow": "OpenFlow",
    "onos": "ONOS",
    "k3s": "k3s",
    "dns": "DNS",
    "dhcp": "DHCP",
    "dhcp_client": "DHCP client",
    "http": "HTTP",
    "load_balancer": "LB",
    "wireguard": "WireGuard",
    "telemetry": "telemetry",
}
CAP_HIDE = {"linux"}


# ----------------------------------------------------------------- model ----


@dataclass
class Topo:
    task_id: str
    network: str
    description: str
    roles: dict[str, str]
    caps: dict[str, list[str]]
    p2p: list[tuple[str, str, str, str]]  # a, port_a, b, port_b
    bus_lans: list[tuple[str, list[tuple[str, str]]]] = field(
        default_factory=list
    )  # hub, members
    oob_lans: list[list[tuple[str, str]]] = field(default_factory=list)


def natural_key(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def load_task(task_dir: Path) -> Topo:
    data = json.loads((task_dir / "task.json").read_text())
    roles = {d["name"]: d.get("role", "unknown") for d in data.get("devices", [])}
    caps = {d["name"]: list(d.get("capabilities", [])) for d in data.get("devices", [])}
    p2p, lans = [], []
    for seg in data.get("segments", []):
        ends = [(e["device"], e.get("port", "")) for e in seg]
        for dev, _ in ends:
            roles.setdefault(dev, "unknown")
            caps.setdefault(dev, [])
        if len(ends) == 2:
            (a, pa), (b, pb) = ends
            p2p.append((a, pa, b, pb))
        elif len(ends) > 2:
            lans.append(ends)
    topo = Topo(
        task_id=data.get("task_id", task_dir.name),
        network=data.get("network", task_dir.name),
        description=data.get("description", ""),
        roles={k: (v if v in ROLE_STYLE else "unknown") for k, v in roles.items()},
        caps=caps,
        p2p=p2p,
    )
    # Count attachments per device to classify shared LANs.
    attach: dict[str, int] = {d: 0 for d in roles}
    for a, _, b, _ in p2p:
        attach[a] += 1
        attach[b] += 1
    for lan in lans:
        for dev, _ in lan:
            attach[dev] += 1
    for lan in lans:
        external = [dev for dev, _ in lan if attach[dev] > 1]
        if len(external) <= 1:
            hub = external[0] if external else lan[0][0]
            topo.bus_lans.append((hub, lan))
        else:
            topo.oob_lans.append(lan)
    return topo


def short_description(text: str, limit: int = 150) -> str:
    text = re.sub(r"^\s*Network Description:\s*", "", text)
    text = " ".join(text.split())
    m = re.search(r"(?<![A-Z.][a-z])\.(\s|$)", text)
    sentence = text[: m.start() + 1] if m else text
    if len(sentence) > limit:
        sentence = sentence[:limit].rsplit(" ", 1)[0].rstrip(",;:") + " …"
    return sentence


def caps_text(caps: list[str]) -> str:
    shown = [CAP_LABELS.get(c, c) for c in caps if c not in CAP_HIDE]
    s = set(caps)
    if {"p4", "bmv2"} <= s:  # compact the programmable-switch stack
        shown = [x for x in shown if x not in ("P4", "BMv2", "P4Runtime")]
        shown.insert(0, "P4/BMv2")
    if "frr" in s and len(shown) > 1:
        shown = [x for x in shown if x != "FRR"]
    return " · ".join(shown)


# ---------------------------------------------------------------- layout ----


def build_graph(topo: Topo) -> nx.Graph:
    g = nx.Graph()
    g.add_nodes_from(topo.roles)
    for a, _, b, _ in topo.p2p:
        g.add_edge(a, b)
    for hub, lan in topo.bus_lans:
        for dev, _ in lan:
            if dev != hub:
                g.add_edge(hub, dev)
    return g


def is_forwarding(topo: Topo, g: nx.Graph, n: str) -> bool:
    return topo.roles[n] in ("router", "switch") or g.degree(n) >= 2


def keyword_rank(name: str) -> int | None:
    ranks = [
        TIER_KEYWORDS[t]
        for t in re.split(r"[_\-\d]+", name.lower())
        if t in TIER_KEYWORDS
    ]
    return min(ranks) if ranks else None


def assign_depths(topo: Topo, g: nx.Graph, nodes: list[str]) -> dict[str, int]:
    sub = g.subgraph(nodes)
    fwd = [n for n in nodes if is_forwarding(topo, g, n)]
    depth: dict[str, int] = {}
    for comp in nx.connected_components(sub):
        comp = set(comp)
        cf = [n for n in fwd if n in comp] or sorted(comp, key=natural_key)
        ranked = {n: keyword_rank(n) for n in cf if keyword_rank(n) is not None}
        if ranked:
            best = min(ranked.values())
            top = [n for n, r in ranked.items() if r == best]
        else:
            ends = [n for n in comp if sub.degree(n) <= 1] or cf
            height = nx.multi_source_dijkstra_path_length(sub, ends)
            hmax = max(height[n] for n in cf)
            top = [n for n in cf if height[n] == hmax]
        d = nx.multi_source_dijkstra_path_length(sub.subgraph(comp), top)
        # End hosts hanging directly off the top tier are drawn above it.
        for n in comp:
            if sub.degree(n) == 1 and not is_forwarding(topo, g, n):
                (nb,) = list(sub.neighbors(n))
                if d.get(nb) == 0:
                    d[n] = -1
        depth.update(d)
    return depth


def is_mesh(topo: Topo, g: nx.Graph, depth: dict[str, int]) -> bool:
    ff = [
        (a, b)
        for a, b in g.edges()
        if is_forwarding(topo, g, a) and is_forwarding(topo, g, b)
    ]
    if len(ff) < 4:
        return False
    same = sum(1 for a, b in ff if depth.get(a) == depth.get(b))
    return same / len(ff) > 0.5


def isotonic(v: list[float]) -> list[float]:
    """Pool-adjacent-violators: nondecreasing least-squares fit."""
    blocks: list[list[float]] = []  # [sum, count]
    for x in v:
        blocks.append([x, 1])
        while (
            len(blocks) > 1
            and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]
        ):
            s, c = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += c
    out = []
    for s, c in blocks:
        out += [s / c] * c
    return out


def place_row(desired: list[float], gaps: list[float]) -> list[float]:
    off = [0.0]
    for gp in gaps:
        off.append(off[-1] + gp)
    y = isotonic([d - o for d, o in zip(desired, off)])
    return [a + o for a, o in zip(y, off)]


def count_crossings(order: dict[int, list[str]], g: nx.Graph, depth) -> int:
    total = 0
    tiers = sorted(order)
    for t in tiers[:-1]:
        if t + 1 not in order:
            continue
        pos_a = {n: i for i, n in enumerate(order[t])}
        pos_b = {n: i for i, n in enumerate(order[t + 1])}
        edges = [
            (pos_a[a], pos_b[b]) for a in order[t] for b in g.neighbors(a) if b in pos_b
        ]
        for i in range(len(edges)):
            for j in range(i + 1, len(edges)):
                (a1, b1), (a2, b2) = edges[i], edges[j]
                if (a1 - a2) * (b1 - b2) < 0:
                    total += 1
    return total


def layered_layout(g, depth, footprint, avail_w):
    tiers = sorted(set(depth.values()))
    order = {
        t: sorted([n for n in depth if depth[n] == t], key=natural_key) for t in tiers
    }

    def sweep(direction):
        seq = tiers[1:] if direction > 0 else tiers[-2::-1]
        for t in seq:
            ref = t - direction
            if ref not in order:
                continue
            idx = {n: i for i, n in enumerate(order[ref])}
            cur = {n: i for i, n in enumerate(order[t])}

            def key(n):
                nb = [idx[m] for m in g.neighbors(n) if m in idx]
                return (sum(nb) / len(nb) if nb else cur[n], cur[n])

            order[t] = sorted(order[t], key=key)

    best = ({t: list(v) for t, v in order.items()}, count_crossings(order, g, depth))
    for _ in range(6):
        sweep(+1)
        sweep(-1)
        c = count_crossings(order, g, depth)
        if c < best[1]:
            best = ({t: list(v) for t, v in order.items()}, c)
    order = best[0]

    # x placement: rows with minimum gaps, pulled toward neighbours.
    gap_pad = 18
    widest = max(
        sum(footprint[n] for n in order[t]) + gap_pad * (len(order[t]) - 1)
        for t in tiers
    )
    target = min(avail_w, max(widest, avail_w * 0.6))

    def gaps_for(t):
        row = order[t]
        spread = 0.55 * target / max(len(row), 1) if len(row) > 1 else 0
        return [
            max((footprint[a] + footprint[b]) / 2 + gap_pad, spread)
            for a, b in zip(row, row[1:])
        ]

    x: dict[str, float] = {}
    for t in tiers:
        gp = gaps_for(t)
        xs = place_row([0.0] * len(order[t]), gp)
        for n, v in zip(order[t], xs):
            x[n] = v

    # Anchor the widest tier, then place the other tiers outward from it, each
    # node centred over its already placed neighbours.
    def row_width(t):
        return sum(footprint[n] for n in order[t]) + sum(gaps_for(t))

    anchor = max(tiers, key=row_width)
    placed = set(order[anchor])
    seq = [t for t in tiers if t < anchor][::-1] + [t for t in tiers if t > anchor]
    for t in seq:
        ref = t + 1 if t < anchor else t - 1
        des = []
        for n in order[t]:
            nb = [x[m] for m in g.neighbors(n) if m in placed and depth.get(m) == ref]
            nb = nb or [x[m] for m in g.neighbors(n) if m in placed]
            des.append(sum(nb) / len(nb) if nb else None)
        known = [d for d in des if d is not None]
        centre = sum(known) / len(known) if known else 0.0
        if not known:
            des = [x[n] + centre for n in order[t]]
        else:  # unconstrained nodes keep their slot relative to their row neighbours
            for i, d in enumerate(des):
                if d is None:
                    left = next(
                        (des[j] for j in range(i - 1, -1, -1) if des[j] is not None),
                        None,
                    )
                    des[i] = (left + 1) if left is not None else centre
        for n, v in zip(order[t], place_row(des, gaps_for(t))):
            x[n] = v
        placed.update(order[t])
    return order, x


def mesh_layout(topo, g, nodes):
    fwd = [n for n in nodes if is_forwarding(topo, g, n)]
    core = g.subgraph(fwd)
    pos = nx.kamada_kawai_layout(core) if len(fwd) > 1 else {fwd[0]: np.zeros(2)}
    pts = np.array([pos[n] for n in fwd], dtype=float)
    pts -= pts.mean(axis=0)
    if len(fwd) > 2:  # rotate the principal axis horizontal to fit 16:9
        _, _, vt = np.linalg.svd(pts, full_matrices=False)
        pts = pts @ vt.T
    return {n: pts[i] for i, n in enumerate(fwd)}


# --------------------------------------------------------------- drawing ----


class Canvas:
    def __init__(self):
        self.fig = plt.figure(figsize=(W / DPI, H / DPI), dpi=DPI, facecolor="white")
        self.ax = self.fig.add_axes([0, 0, 1, 1])
        self.ax.set_xlim(0, W)
        self.ax.set_ylim(0, H)
        self.ax.set_axis_off()
        self.renderer = self.fig.canvas.get_renderer()
        self.boxes: list[tuple[float, float, float, float]] = []  # occupied x0,y0,x1,y1

    def measure(self, text, size, weight="normal"):
        t = self.ax.text(0, 0, text, fontsize=size, fontfamily=FONT, fontweight=weight)
        bb = t.get_window_extent(self.renderer)
        t.remove()
        return bb.width, bb.height

    def text(self, x, y, s, size, register=True, **kw):
        t = self.ax.text(x, y, s, fontsize=size, fontfamily=FONT, **kw)
        bb = t.get_window_extent(self.renderer)
        box = (bb.x0, bb.y0, bb.x1, bb.y1)
        if register:
            self.boxes.append(box)
        return t, box

    def free(self, box, pad=1.5):
        x0, y0, x1, y1 = box
        if x0 < 2 or x1 > W - 2 or y0 < 2 or y1 > H - 2:
            return False
        for a0, b0, a1, b1 in self.boxes:
            if x0 < a1 + pad and x1 > a0 - pad and y0 < b1 + pad and y1 > b0 - pad:
                return False
        return True


def draw_arrow(ax, x, y, dx, dy, color, width, z):
    ax.add_patch(
        FancyArrow(
            x,
            y,
            dx,
            dy,
            width=width,
            head_width=width * 2.6,
            head_length=width * 2.2,
            length_includes_head=True,
            color=color,
            lw=0,
            zorder=z,
        )
    )


def draw_icon(ax, role, x, y, s=ICON, z=4):
    c = ROLE_STYLE[role]["color"]
    white = "white"
    if role == "router":
        r = s * 0.48
        ax.add_patch(
            Circle((x, y), r, facecolor=c, edgecolor="#123f66", lw=1.2, zorder=z)
        )
        a, w = r * 0.62, r * 0.13
        # two arrows pointing in (left/right), two pointing out (up/down)
        draw_arrow(ax, x - r * 0.95, y + r * 0.0, a * 0.95, 0, white, w, z + 1)
        draw_arrow(ax, x + r * 0.95, y, -a * 0.95, 0, white, w, z + 1)
        draw_arrow(ax, x, y + r * 0.08, 0, a * 0.9, white, w, z + 1)
        draw_arrow(ax, x, y - r * 0.08, 0, -a * 0.9, white, w, z + 1)
    elif role == "switch":
        w_, h_ = s * 1.1, s * 0.62
        ax.add_patch(
            FancyBboxPatch(
                (x - w_ / 2, y - h_ / 2),
                w_,
                h_,
                boxstyle="round,pad=0,rounding_size=4",
                facecolor=c,
                edgecolor="#135c50",
                lw=1.2,
                zorder=z,
            )
        )
        aw = h_ * 0.11
        draw_arrow(ax, x - w_ * 0.32, y + h_ * 0.18, w_ * 0.64, 0, white, aw, z + 1)
        draw_arrow(ax, x + w_ * 0.32, y - h_ * 0.18, -w_ * 0.64, 0, white, aw, z + 1)
    elif role == "host":
        sw, sh = s * 0.95, s * 0.62
        top = y + s * 0.42
        ax.add_patch(
            FancyBboxPatch(
                (x - sw / 2, top - sh),
                sw,
                sh,
                boxstyle="round,pad=0,rounding_size=3",
                facecolor=c,
                edgecolor="#2d3340",
                lw=1.2,
                zorder=z,
            )
        )
        m = s * 0.07
        ax.add_patch(
            Rectangle(
                (x - sw / 2 + m, top - sh + m),
                sw - 2 * m,
                sh - 2 * m,
                facecolor="#dbe8f5",
                lw=0,
                zorder=z + 1,
            )
        )
        ax.add_patch(
            Rectangle(
                (x - s * 0.06, top - sh - s * 0.16),
                s * 0.12,
                s * 0.16,
                facecolor=c,
                lw=0,
                zorder=z,
            )
        )
        ax.add_patch(
            FancyBboxPatch(
                (x - s * 0.3, top - sh - s * 0.22),
                s * 0.6,
                s * 0.07,
                boxstyle="round,pad=0,rounding_size=2",
                facecolor=c,
                lw=0,
                zorder=z,
            )
        )
    elif role in ("service", "unknown"):
        tw, th = s * 0.58, s * 0.92
        ax.add_patch(
            FancyBboxPatch(
                (x - tw / 2, y - th / 2),
                tw,
                th,
                boxstyle="round,pad=0,rounding_size=3",
                facecolor=c,
                edgecolor="#7a4512" if role == "service" else "#555",
                lw=1.2,
                zorder=z,
            )
        )
        for k in range(3):
            yy = y + th * 0.3 - k * th * 0.16
            ax.add_patch(
                Rectangle(
                    (x - tw * 0.32, yy - 1.2),
                    tw * 0.64,
                    2.4,
                    facecolor=white,
                    lw=0,
                    zorder=z + 1,
                )
            )
        ax.add_patch(
            Circle(
                (x + tw * 0.2, y - th * 0.3),
                s * 0.045,
                facecolor=white,
                lw=0,
                zorder=z + 1,
            )
        )
    elif role == "infrastructure":
        rw, rh = s * 1.0, s * 0.27
        for k in range(2):
            yy = y + (0.5 - k) * (rh + 3)
            ax.add_patch(
                FancyBboxPatch(
                    (x - rw / 2, yy - rh / 2),
                    rw,
                    rh,
                    boxstyle="round,pad=0,rounding_size=2.5",
                    facecolor=c,
                    edgecolor="#3b4d59",
                    lw=1.1,
                    zorder=z,
                )
            )
            ax.add_patch(
                Circle(
                    (x + rw * 0.33, yy),
                    s * 0.04,
                    facecolor="#9ff0a8",
                    lw=0,
                    zorder=z + 1,
                )
            )
            ax.add_patch(
                Rectangle(
                    (x - rw * 0.38, yy - 1),
                    rw * 0.45,
                    2,
                    facecolor=white,
                    lw=0,
                    zorder=z + 1,
                )
            )
    elif role == "controller":
        r = s * 0.5
        pts = [
            (
                x + r * math.cos(math.radians(30 + 60 * k)),
                y + r * math.sin(math.radians(30 + 60 * k)),
            )
            for k in range(6)
        ]
        ax.add_patch(
            Polygon(
                pts, closed=True, facecolor=c, edgecolor="#4a2d66", lw=1.2, zorder=z
            )
        )
        ax.add_patch(
            Circle(
                (x, y),
                r * 0.36,
                facecolor="none",
                edgecolor=white,
                lw=2.2,
                zorder=z + 1,
            )
        )
        for k in range(3):
            ang = math.radians(90 + 120 * k)
            ax.plot(
                [x + r * 0.36 * math.cos(ang), x + r * 0.7 * math.cos(ang)],
                [y + r * 0.36 * math.sin(ang), y + r * 0.7 * math.sin(ang)],
                color=white,
                lw=2.2,
                zorder=z + 1,
                solid_capstyle="round",
            )


def icon_half(role, s=ICON):
    """Half width/height of the icon footprint."""
    return {
        "router": (s * 0.5, s * 0.5),
        "switch": (s * 0.57, s * 0.33),
        "host": (s * 0.5, s * 0.45),
        "service": (s * 0.31, s * 0.48),
        "unknown": (s * 0.31, s * 0.48),
        "infrastructure": (s * 0.52, s * 0.33),
        "controller": (s * 0.46, s * 0.5),
    }[role]


def wrap_name(name: str, max_chars: int) -> str:
    if len(name) <= max_chars or "_" not in name:
        return name
    parts = name.split("_")
    best, best_len = name, len(name)
    for i in range(1, len(parts)):
        a, b = "_".join(parts[:i]) + "_", "_".join(parts[i:])
        if max(len(a), len(b)) < best_len:
            best, best_len = a + "\n" + b, max(len(a), len(b))
    return best


@dataclass
class NodeLabel:
    name: str
    caps: str
    width: float
    height: float


def make_labels(cv: Canvas, topo: Topo, nodes, max_chars=20) -> dict[str, NodeLabel]:
    out = {}
    for n in nodes:
        name = wrap_name(n, max_chars)
        cap = caps_text(topo.caps.get(n, []))
        if len(cap) > 18:
            cap = "\n".join(textwrap.wrap(cap.replace(" · ", "·"), 18)).replace(
                "·", " · "
            )
        w1, h1 = cv.measure(name, NAME_PT, "bold")
        w2, h2 = cv.measure(cap, CAPS_PT) if cap else (0, 0)
        out[n] = NodeLabel(name, cap, max(w1, w2), h1 + h2 + (2 if cap else 0))
    return out


def render(topo: Topo, out_path: Path) -> list[str]:
    warnings: list[str] = []
    cv = Canvas()
    ax = cv.ax
    g = build_graph(topo)

    side_nodes = [n for n in g.nodes if g.degree(n) == 0]
    main_nodes = [n for n in g.nodes if g.degree(n) > 0]
    has_side = bool(side_nodes or topo.oob_lans)
    x_left = MARGIN_X
    x_right = W - MARGIN_X - (SIDE_W if has_side else 0)
    y_top = H - TITLE_H - 42
    y_bot = LEGEND_H + 66

    labels = make_labels(cv, topo, g.nodes)
    footprint = {
        n: max(2 * icon_half(topo.roles[n])[0], labels[n].width) for n in g.nodes
    }

    depth = assign_depths(topo, g, main_nodes)
    label_up: set[str] = set()
    pos: dict[str, tuple[float, float]] = {}
    mesh = is_mesh(topo, g, depth)
    if mesh:
        core = mesh_layout(topo, g, main_nodes)
        arr = np.array(list(core.values()))
        span = arr.max(axis=0) - arr.min(axis=0)
        span[span == 0] = 1
        pad_x, pad_y = 110, 85
        sx = (x_right - x_left - 2 * pad_x) / span[0]
        sy = (y_top - y_bot - 2 * pad_y) / span[1]
        lo = arr.min(axis=0)
        for n, p in core.items():
            pos[n] = (
                x_left + pad_x + (p[0] - lo[0]) * sx,
                y_bot + pad_y + (p[1] - lo[1]) * sy,
            )

        # place endpoints around their attachment point, in the freest spot
        def node_box(n, x, y, up=False):
            hw, hh = icon_half(topo.roles[n])
            lw = max(labels[n].width, 2 * hw) / 2
            if up:
                return (
                    x - lw - 4,
                    y - hh - 4,
                    x + lw + 4,
                    y + hh + labels[n].height + 8,
                )
            return (x - lw - 4, y - hh - labels[n].height - 8, x + lw + 4, y + hh + 4)

        def overlap(b1, b2):
            return max(0.0, min(b1[2], b2[2]) - max(b1[0], b2[0])) * max(
                0.0, min(b1[3], b2[3]) - max(b1[1], b2[1])
            )

        def box_hits_segment(b, p, q):
            for k in range(1, 30):
                t = k / 30
                x, y = p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t
                if b[0] <= x <= b[2] and b[1] <= y <= b[3]:
                    return True
            return False

        cx = np.mean([p[0] for p in pos.values()])
        cy = np.mean([p[1] for p in pos.values()])
        boxes = {n: node_box(n, *pos[n]) for n in pos}
        for n in sorted(main_nodes, key=natural_key):
            if n in pos:
                continue
            nbs = [m for m in g.neighbors(n) if m in pos]
            if not nbs:
                continue
            ax0, ay0 = pos[nbs[0]]
            taken = [
                math.atan2(pos[m][1] - ay0, pos[m][0] - ax0)
                for m in g.neighbors(nbs[0])
                if m in pos
            ]
            taken.append(-math.pi / 2)  # the attachment point's own label sits below it
            outward = math.atan2(ay0 - cy, ax0 - cx)
            segs = [(pos[a], pos[b]) for a, b in g.edges() if a in pos and b in pos]
            best, best_score = None, -1e18
            for dist in (78, 100, 125):
                for k in range(36):
                    a = 2 * math.pi * k / 36
                    cand = (ax0 + dist * math.cos(a), ay0 + dist * 0.8 * math.sin(a))
                    up = math.sin(a) > 0.3  # above its router: put the label on top
                    b = node_box(n, *cand, up)
                    gap = min(
                        abs((a - t + math.pi) % (2 * math.pi) - math.pi) for t in taken
                    )
                    score = gap + 0.3 * math.cos(a - outward) - dist / 200
                    score -= sum(overlap(b, ob) for ob in boxes.values()) / 50
                    score -= 2.0 * sum(box_hits_segment(b, p, q) for p, q in segs)
                    if not (
                        x_left < b[0]
                        and b[2] < x_right
                        and y_bot - 30 < b[1]
                        and b[3] < y_top + 20
                    ):
                        score -= 50
                    if score > best_score:
                        best, best_score, best_up = cand, score, up
            pos[n] = best
            boxes[n] = node_box(n, *best, best_up)
            if best_up:
                label_up.add(n)
    else:
        avail = x_right - x_left
        for max_chars in (20, 12):  # wrap long names only when the drawing is too wide
            if max_chars != 20:
                labels = make_labels(cv, topo, g.nodes, max_chars)
                footprint = {
                    n: max(2 * icon_half(topo.roles[n])[0], labels[n].width)
                    for n in g.nodes
                }
            order, xs = layered_layout(g, depth, footprint, avail)
            lo = min(xs[n] - footprint[n] / 2 for n in xs)
            hi = max(xs[n] + footprint[n] / 2 for n in xs)
            scale = min(1.7, avail / (hi - lo)) if hi > lo else 1.0
            if scale >= 1.0:
                break
        tiers = sorted(order)
        # headroom for arcs between non-adjacent devices of the top tier
        top_row = set(order[tiers[0]])
        top_idx = {n: i for i, n in enumerate(order[tiers[0]])}
        if any(
            a in top_row and b in top_row and abs(top_idx[a] - top_idx[b]) > 1
            for a, b in g.edges()
        ):
            y_top -= 50
        if scale < 1.0:
            warnings.append(f"layout compressed to {scale:.2f}")
        mid = (lo + hi) / 2
        cxm = (x_left + x_right) / 2
        step = (y_top - y_bot) / max(len(tiers) - 1, 1)
        step = min(step, 230)
        # Labels go above nodes drawn above the top tier and above nodes that fan
        # out to three or more lower devices (a shared LAN counts once).
        bus_edges = {(hub, d) for hub, lan in topo.bus_lans for d, _ in lan if d != hub}
        for n in depth:
            lower = sum(
                1
                for m in g.neighbors(n)
                if depth.get(m, -9) > depth[n] and (n, m) not in bus_edges
            ) + sum(1 for hub, _ in topo.bus_lans if hub == n)
            upper = sum(1 for m in g.neighbors(n) if depth.get(m, 99) < depth[n])
            if depth[n] == -1 or (lower >= 3 and lower >= 2 * upper + 1):
                label_up.add(n)
        if label_up & set(order[tiers[0]]):
            step = min(step, (y_top - 34 - y_bot) / max(len(tiers) - 1, 1))
            y_top -= 34
        total = step * (len(tiers) - 1)
        y0 = (y_top + y_bot) / 2 + total / 2
        for i, t in enumerate(tiers):
            for n in order[t]:
                pos[n] = (cxm + (xs[n] - mid) * scale, y0 - i * step)

    # side panel: out-of-band LANs and devices attached only to them
    lan_letter = {}
    side_pos = {}
    if has_side:
        px = W - SIDE_W + 10
        groups = []
        for i, lan in enumerate(topo.oob_lans):
            letter = chr(ord("A") + i)
            lan_letter[i] = letter
            only = [d for d, _ in lan if d in side_nodes]
            groups.append((letter, i, only))
        placed = {d for _, _, only in groups for d in only}
        rest = [n for n in side_nodes if n not in placed]
        if rest:
            groups.append((None, None, rest))
        slots = sum(max(len(o), 1) + 1.2 for _, _, o in groups)
        unit = min((y_top - y_bot + 40) / max(slots, 1), 120)
        y = y_top + 20
        for letter, i, only in groups:
            color = LAN_COLORS[i % len(LAN_COLORS)] if i is not None else LINK_COLOR
            head_y = y
            if letter:
                n_tag = sum(1 for d, _ in topo.oob_lans[i] if d not in side_nodes)
                cv.text(
                    px,
                    head_y,
                    f"Shared LAN {letter}",
                    11,
                    color=color,
                    fontweight="bold",
                    va="center",
                )
                cv.text(
                    px,
                    head_y - 17,
                    f"+ {n_tag} devices tagged “{letter}:port”",
                    8.5,
                    color="#555",
                    va="center",
                )
            else:
                cv.text(
                    px,
                    head_y,
                    "Unconnected",
                    11,
                    color=color,
                    fontweight="bold",
                    va="center",
                )
            bus_x = px + 12
            ys = [head_y - unit * (k + 1.15) for k in range(max(len(only), 1))]
            if letter:
                ax.plot(
                    [bus_x, bus_x],
                    [head_y - 28, min(ys) - 8],
                    color=color,
                    lw=4,
                    solid_capstyle="round",
                    zorder=2,
                )
            for d, yy in zip(only, ys):
                side_pos[d] = (bus_x + 62, yy)
                if letter:
                    ax.plot(
                        [bus_x, bus_x + 62], [yy, yy], color=color, lw=1.6, zorder=1
                    )
                    port = next(p for dd, p in topo.oob_lans[i] if dd == d)
                    cv.text(
                        bus_x + 6, yy + 3, port, PORT_PT, color=PORT_COLOR, va="bottom"
                    )
            y = min(ys) - unit * 0.9
        pos.update(side_pos)

    # register icon boxes first so labels avoid every icon
    for n, (x, y) in pos.items():
        hw, hh = icon_half(topo.roles[n])
        cv.boxes.append((x - hw, y - hh, x + hw, y + hh))

    # links
    def same_tier_arc(a, b):
        (x1, y1), (x2, y2) = pos[a], pos[b]
        if abs(y1 - y2) > 1 or mesh:
            return None
        row = sorted(
            (
                p
                for p in pos.values()
                if abs(p[1] - y1) < 1 and p not in side_pos.values()
            )
        )
        between = [p for p in row if min(x1, x2) < p[0] < max(x1, x2)]
        if not between:
            return None
        hgt = min(22 + 0.1 * abs(x2 - x1), 42)
        return MPath(
            [(x1, y1 + 10), ((x1 + x2) / 2, y1 + hgt * 2), (x2, y2 + 10)],
            [MPath.MOVETO, MPath.CURVE3, MPath.CURVE3],
        )

    link_ends = []  # (node, port, other_point)
    for a, pa, b, pb in topo.p2p:
        if a not in pos or b not in pos:
            continue
        arc = same_tier_arc(a, b)
        if arc is not None:
            ax.add_patch(
                PathPatch(arc, facecolor="none", edgecolor=LINK_COLOR, lw=1.6, zorder=1)
            )
            (x1, y1), (x2, y2) = pos[a], pos[b]
            mx = (x1 + x2) / 2
            link_ends.append((a, pa, (mx, y1 + 200)))
            link_ends.append((b, pb, (mx, y2 + 200)))
        else:
            (x1, y1), (x2, y2) = pos[a], pos[b]
            ax.plot(
                [x1, x2],
                [y1, y2],
                color=LINK_COLOR,
                lw=1.6,
                zorder=1,
                solid_capstyle="round",
            )
            link_ends.append((a, pa, pos[b]))
            link_ends.append((b, pb, pos[a]))

    for hub, lan in topo.bus_lans:
        members = [(d, p) for d, p in lan if d != hub and d in pos]
        if hub not in pos or not members:
            continue
        hx, hy = pos[hub]
        my = (hy + min(pos[d][1] for d, _ in members)) / 2
        xs_ = [pos[d][0] for d, _ in members] + [hx]
        ax.plot(
            [min(xs_) - 8, max(xs_) + 8],
            [my, my],
            color="#2d3340",
            lw=4,
            solid_capstyle="round",
            zorder=2,
        )
        ax.plot([hx, hx], [hy, my], color=LINK_COLOR, lw=1.6, zorder=1)
        hub_port = next(p for d, p in lan if d == hub)
        link_ends.append((hub, hub_port, (hx, my)))
        for d, p in members:
            ax.plot(
                [pos[d][0], pos[d][0]],
                [my, pos[d][1]],
                color=LINK_COLOR,
                lw=1.6,
                zorder=1,
            )
            link_ends.append((d, p, (pos[d][0], my)))

    # icons
    for n, (x, y) in pos.items():
        draw_icon(ax, topo.roles[n], x, y)

    # names (+ capability line) below icons, or above for nodes drawn above the top tier
    for n, (x, y) in pos.items():
        lab = labels[n]
        hw, hh = icon_half(topo.roles[n])
        above = n in label_up
        if n in side_pos:
            yy = y - hh - 3
            t, box = cv.text(
                x,
                yy,
                lab.name,
                NAME_PT,
                ha="center",
                va="top",
                fontweight="bold",
                color="#1d2330",
                zorder=6,
            )
        elif above:
            yy = y + hh + 3
            if lab.caps:
                _, cb = cv.text(
                    x,
                    yy,
                    lab.caps,
                    CAPS_PT,
                    ha="center",
                    va="bottom",
                    color="#5a6270",
                    zorder=6,
                    linespacing=1.0,
                    bbox=dict(facecolor="white", edgecolor="none", pad=0.6, alpha=0.9),
                )
                yy = cb[3] + 1
            t, box = cv.text(
                x,
                yy,
                lab.name,
                NAME_PT,
                ha="center",
                va="bottom",
                fontweight="bold",
                color="#1d2330",
                zorder=6,
                bbox=dict(facecolor="white", edgecolor="none", pad=0.6, alpha=0.9),
            )
            continue
        else:
            yy = y - hh - 3
            t, box = cv.text(
                x,
                yy,
                lab.name,
                NAME_PT,
                ha="center",
                va="top",
                fontweight="bold",
                color="#1d2330",
                zorder=6,
                linespacing=1.0,
                bbox=dict(facecolor="white", edgecolor="none", pad=0.6, alpha=0.9),
            )
        if lab.caps:
            cv.text(
                x,
                box[1] - 1,
                lab.caps,
                CAPS_PT,
                ha="center",
                va="top",
                color="#5a6270",
                zorder=6,
                linespacing=1.0,
                bbox=dict(facecolor="white", edgecolor="none", pad=0.6, alpha=0.9),
            )

    # LAN tags on members of out-of-band LANs that sit in the main drawing
    for i, lan in enumerate(topo.oob_lans):
        color = LAN_COLORS[i % len(LAN_COLORS)]
        for d, p in lan:
            if d in side_pos or d not in pos:
                continue
            x, y = pos[d]
            hw, hh = icon_half(topo.roles[d])
            text = f"{lan_letter[i]}:{p}"
            tw, th = cv.measure(text, TAG_PT, "bold")
            done = False
            for dx, dy, ha, va in [
                (hw + 7, 1, "left", "bottom"),
                (hw + 7, -1, "left", "top"),
                (-hw - 7, 1, "right", "bottom"),
                (-hw - 7, -1, "right", "top"),
                (0, hh + 6, "center", "bottom"),
            ]:
                bx = x + dx - (tw if ha == "right" else tw / 2 if ha == "center" else 0)
                by = y + dy - (th if va == "top" else 0)
                box = (bx - 3, by - 2, bx + tw + 3, by + th + 2)
                if cv.free(box, pad=0.5):
                    cv.text(
                        x + dx,
                        y + dy,
                        text,
                        TAG_PT,
                        ha=ha,
                        va=va,
                        color="white",
                        fontweight="bold",
                        zorder=7,
                        bbox=dict(
                            boxstyle="round,pad=0.25", facecolor=color, edgecolor="none"
                        ),
                    )
                    done = True
                    break
            if not done:
                warnings.append(f"no room for LAN tag on {d}")

    # port names near link ends, dropped where they would collide
    dropped = 0
    for n, port, (ox, oy) in link_ends:
        if not port:
            continue
        x, y = pos[n]
        dx, dy = ox - x, oy - y
        L = math.hypot(dx, dy) or 1
        ux, uy = dx / L, dy / L
        tw, th = cv.measure(port, PORT_PT)
        hw, hh = icon_half(topo.roles[n])
        r0 = max(hw, hh) + 8
        placed = False
        for r in (r0 + 2, r0 + 8, r0 + 16, r0 + 26, r0 + 38):
            if r > L * 0.5:
                break
            for side in (1, -1):
                px_ = x + ux * r + side * (-uy) * (tw / 2 + 3)
                py_ = y + uy * r + side * ux * (th / 2 + 3)
                box = (
                    px_ - tw / 2 - 1,
                    py_ - th / 2 - 1,
                    px_ + tw / 2 + 1,
                    py_ + th / 2 + 1,
                )
                if cv.free(box, pad=2.5):
                    cv.text(
                        px_,
                        py_,
                        port,
                        PORT_PT,
                        ha="center",
                        va="center",
                        color=PORT_COLOR,
                        zorder=5,
                    )
                    placed = True
                    break
            if placed:
                break
        if not placed:
            dropped += 1
    if dropped:
        warnings.append(f"{dropped}/{len(link_ends)} port labels dropped for space")

    # title
    cv.text(
        MARGIN_X - 10,
        H - 30,
        topo.network,
        19,
        fontweight="bold",
        color="#1d2330",
        va="center",
    )
    cv.text(
        MARGIN_X - 10,
        H - 60,
        short_description(topo.description),
        11.5,
        color="#4a5160",
        va="center",
    )
    ax.plot(
        [MARGIN_X - 10, W - MARGIN_X + 10],
        [H - TITLE_H + 8, H - TITLE_H + 8],
        color="#d5d9e0",
        lw=1,
        zorder=0,
    )

    # legend
    ly = LEGEND_H / 2 + 4
    ax.plot(
        [MARGIN_X - 10, W - MARGIN_X + 10],
        [LEGEND_H + 6, LEGEND_H + 6],
        color="#d5d9e0",
        lw=1,
        zorder=0,
    )
    lx = MARGIN_X
    present = [r for r in ROLE_ORDER if r in set(topo.roles[n] for n in pos)]
    for r in present:
        draw_icon(ax, r, lx + 14, ly, s=26)
        _, box = cv.text(
            lx + 34, ly, ROLE_STYLE[r]["label"], 9.5, va="center", color="#333"
        )
        lx = box[2] + 26
    ax.plot([lx, lx + 34], [ly, ly], color=LINK_COLOR, lw=1.6)
    _, box = cv.text(lx + 42, ly, "link", 9.5, va="center", color="#333")
    lx = box[2] + 26
    if topo.bus_lans:
        ax.plot([lx, lx + 34], [ly, ly], color="#2d3340", lw=4, solid_capstyle="round")
        _, box = cv.text(lx + 42, ly, "shared LAN", 9.5, va="center", color="#333")
        lx = box[2] + 26
    if topo.oob_lans:
        cv.text(
            lx,
            ly,
            "A:eth4",
            TAG_PT,
            va="center",
            color="white",
            fontweight="bold",
            bbox=dict(
                boxstyle="round,pad=0.25", facecolor=LAN_COLORS[0], edgecolor="none"
            ),
        )
        _, box = cv.text(
            lx + 44,
            ly,
            "member of shared LAN A on port eth4",
            9.5,
            va="center",
            color="#333",
        )
        lx = box[2] + 26
    _, box = cv.text(lx, ly, "eth1", PORT_PT, va="center", color=PORT_COLOR)
    cv.text(box[2] + 8, ly, "interface name", 9.5, va="center", color="#333")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv.fig.savefig(out_path, dpi=DPI, facecolor="white")
    plt.close(cv.fig)
    return warnings


# ------------------------------------------------------------------ main ----


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "dataset", nargs="?", type=Path, help="dataset folder containing tasks/"
    )
    ap.add_argument("--task", type=Path, help="draw a single task folder instead")
    args = ap.parse_args(argv)
    if args.task:
        task_dirs = [args.task]
    elif args.dataset:
        root = (
            args.dataset / "tasks"
            if (args.dataset / "tasks").is_dir()
            else args.dataset
        )
        task_dirs = sorted(p for p in root.iterdir() if (p / "task.json").is_file())
    else:
        ap.error("give a dataset folder or --task <task_dir>")
    if not task_dirs:
        print("no task.json found", file=sys.stderr)
        return 1
    for td in task_dirs:
        topo = load_task(td)
        out = td / "topology.png"
        warns = render(topo, out)
        note = f"  ({'; '.join(warns)})" if warns else ""
        print(f"{out}{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
