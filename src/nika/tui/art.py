"""Home-screen art: the NIKA logo and a network "diagram of the day"."""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass

from rich.text import Text

_LOGO = (
    "███╗   ██╗██╗██╗  ██╗ █████╗ ",
    "████╗  ██║██║██║ ██╔╝██╔══██╗",
    "██╔██╗ ██║██║█████╔╝ ███████║",
    "██║╚██╗██║██║██╔═██╗ ██╔══██║",
    "██║ ╚████║██║██║  ██╗██║  ██║",
    "╚═╝  ╚═══╝╚═╝╚═╝  ╚═╝╚═╝  ╚═╝",
)
# cyan -> violet -> magenta, sampled per column
_GRADIENT = ((0, 215, 255), (135, 95, 255), (255, 95, 215))

TAGLINE = "Network Incident benchmarK for AI agents"
# Tagline index of each acronym letter, paired with the centre column of the
# same letter in the logo, so each one takes the colour above it.
_ACRONYM = ((0, 4), (8, 11), (25, 17), (31, 25))


def _blend(t: float) -> str:
    seg = min(int(t * (len(_GRADIENT) - 1)), len(_GRADIENT) - 2)
    local = t * (len(_GRADIENT) - 1) - seg
    a, b = _GRADIENT[seg], _GRADIENT[seg + 1]
    r, g, bl = (round(x + (y - x) * local) for x, y in zip(a, b))
    return f"#{r:02x}{g:02x}{bl:02x}"


def logo() -> Text:
    """The NIKA wordmark with a horizontal colour gradient."""
    width = max(len(line) for line in _LOGO)
    text = Text()
    for line in _LOGO:
        for col, char in enumerate(line):
            shadow = char not in "█ "
            style = "grey42" if shadow else f"bold {_blend(col / (width - 1))}"
            text.append(char, style=style)
        text.append("\n")
    tagline = Text(TAGLINE, style="italic grey62")
    for index, col in _ACRONYM:
        tagline.stylize(f"bold {_blend(col / (width - 1))}", index, index + 1)
    text.append_text(tagline)
    return text


@dataclass(frozen=True)
class Diagram:
    scenario: str
    title: str
    art: str
    # Nodes a probe traverses before it hits the fault, in order.
    path: tuple[str, ...]
    incident: str


DIAGRAMS: tuple[Diagram, ...] = (
    Diagram(
        scenario="dc_clos",
        title="leaf-spine fabric",
        art="""\
   ┌────────┐       ┌────────┐
   │ spine1 │       │ spine2 │
   └─┬───┬──┘       └──┬───┬─┘
     │   └──────┐ ┌────┘   ✗
     │   ┌──────┼─┘        │
     │   │      └────────┐ │
   ┌─┴───┴──┐       ┌────┴─┴─┐
   │ leaf1  │       │ leaf2  │
   └─┬────┬─┘       └─┬────┬─┘
    h1    h2         h3    h4""",
        path=("h1", "leaf1", "spine2"),
        incident="spine2 ↔ leaf2 link down: ECMP loses half its paths",
    ),
    Diagram(
        scenario="campus_lan",
        title="three-tier campus",
        art="""\
              ┌──────┐
              │ core │
              └──┬───┘
        ┌────────┴────────┐
    ┌───┴───┐         ┌───┴───┐
    │ dist1 │         │ dist2 │
    └───┬───┘         └───┬───┘
     ┌──┴───┐          ┌──┴───┐
   ┌─┴──┐ ┌─┴──┐     ┌─┴──┐ ┌─┴──┐
   │acc1│ │acc2│     │acc3│ │acc4│
   └─┬──┘ └─┬──┘     └─┬──┘ └─✗──┘
    pc1    pc2        pc3    pc4""",
        path=("pc1", "acc1", "dist1", "core", "dist2", "acc4"),
        incident="acc4 access port in the wrong VLAN: pc4 is isolated",
    ),
    Diagram(
        scenario="isp_geant",
        title="ISP backbone",
        art="""\
   (AMS)━━━━━━━━━(FRA)━━━━━(VIE)
     ┃ ╲           ┃         ┃
     ┃  ╲          ┃         ✗
     ┃   ╲         ┃         ┃
   (LON)━━(PAR)━━(GEN)━━━━━(MIL)
            ┃                ┃
          (MAD)━━━━━━━━━━━━(ROM)""",
        path=("LON", "AMS", "FRA", "VIE"),
        incident="IS-IS adjacency VIE ↔ MIL stuck: traffic detours via GEN",
    ),
    Diagram(
        scenario="k8s_lab",
        title="Kubernetes overlay",
        art="""\
  ┌─ worker-1 ───────┐   ┌─ worker-2 ───────┐
  │ (web)    (api)   │   │ (db)     (cache) │
  │   └──┬─────┘     │   │   └──┬─────┘     │
  │    cni0          │   │    cni0          │
  └──────┬───────────┘   └──────┬───────────┘
         └───── vxlan ────✗─────┘""",
        path=("web", "worker-1", "vxlan"),
        incident="NetworkPolicy drops api → db: the overlay looks healthy",
    ),
    Diagram(
        scenario="p4_dc_fabric",
        title="P4 programmable fabric",
        art="""\
        ┌──────────────────┐
        │  controller      │
        └──┬────────────┬──┘
           │ P4Runtime  │
   ┌───────┴──┐    ┌────┴─────┐
   │ bmv2-s1  ├─✗──┤ bmv2-s2  │
   └──┬────┬──┘    └──┬────┬──┘
     h1    h2        h3    h4""",
        path=("h1", "bmv2-s1"),
        incident="stale table entry on bmv2-s1 blackholes h1 → h3",
    ),
)


def diagram_of_the_day(day: _dt.date | None = None) -> Diagram:
    return DIAGRAMS[(day or _dt.date.today()).toordinal() % len(DIAGRAMS)]


def render_diagram(diagram: Diagram, lit: int | None = None) -> Text:
    """Render *diagram* with the first *lit* path nodes highlighted.

    ``lit=None`` lights the whole path and the fault marker.
    """
    steps = len(diagram.path) if lit is None else lit
    text = Text(diagram.art, style="grey50")
    for node in re.findall(r"[A-Za-z][\w-]*", diagram.art):
        for match in re.finditer(rf"(?<![\w-]){re.escape(node)}(?![\w-])", diagram.art):
            text.stylize("bright_white", match.start(), match.end())
    for index, node in enumerate(diagram.path[:steps]):
        style = (
            "bold green" if index < steps - 1 or lit is None else "bold black on green"
        )
        for match in re.finditer(rf"(?<![\w-]){re.escape(node)}(?![\w-])", diagram.art):
            text.stylize(style, match.start(), match.end())
    if lit is None or lit > len(diagram.path):
        for match in re.finditer("✗", diagram.art):
            text.stylize("bold red blink", match.start(), match.end())
    return text
