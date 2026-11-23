# /// script
# requires-python = ">=3.10"
# dependencies = ["python-pptx>=1.0"]
# ///
"""Draw the README figures as PowerPoint slides, then render them to PNG.

    uv run scripts/offline_recording/figures/make_figures.py

Writes ``overview.pptx``/``.png`` and ``timeline.pptx``/``.png`` next to this
script. Rendering needs LibreOffice (``soffice``) on the PATH.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

HERE = Path(__file__).parent
FONT = "Liberation Sans"

INK = RGBColor(0x1F, 0x29, 0x37)
MUTED = RGBColor(0x6B, 0x72, 0x80)
LINE = RGBColor(0xD1, 0xD5, 0xDB)
GREEN = RGBColor(0x10, 0xB9, 0x81)
GREEN_BG = RGBColor(0xD1, 0xFA, 0xE5)
RED = RGBColor(0xEF, 0x44, 0x44)
RED_BG = RGBColor(0xFE, 0xE2, 0xE2)
AMBER = RGBColor(0xF5, 0x9E, 0x0B)
BLUE = RGBColor(0x25, 0x63, 0xEB)
BLUE_BG = RGBColor(0xDB, 0xEA, 0xFE)
VIOLET = RGBColor(0x7C, 0x3A, 0xED)
VIOLET_BG = RGBColor(0xED, 0xE9, 0xFE)
TEAL = RGBColor(0x0D, 0x94, 0x88)
TEAL_BG = RGBColor(0xCC, 0xFB, 0xF1)
GRAY_BG = RGBColor(0xF3, 0xF4, 0xF6)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)


def new_slide():
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    return prs, slide


def text(
    slide,
    x,
    y,
    w,
    h,
    s,
    size=14,
    color=INK,
    bold=False,
    align=PP_ALIGN.LEFT,
    anchor=MSO_ANCHOR.TOP,
    italic=False,
):
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = Inches(0.02)
    tf.margin_top = tf.margin_bottom = Inches(0.0)
    for i, line in enumerate(s.split("\n")):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        run = p.add_run()
        run.text = line
        f = run.font
        f.name, f.size, f.bold, f.italic = FONT, Pt(size), bold, italic
        f.color.rgb = color
    return box


def box(
    slide,
    x,
    y,
    w,
    h,
    fill,
    line=None,
    shape=MSO_SHAPE.ROUNDED_RECTANGLE,
    radius=0.12,
    label=None,
    size=13,
    color=INK,
    bold=False,
    align=PP_ALIGN.CENTER,
):
    shp = slide.shapes.add_shape(shape, Inches(x), Inches(y), Inches(w), Inches(h))
    if shape == MSO_SHAPE.ROUNDED_RECTANGLE:
        shp.adjustments[0] = radius
    shp.fill.solid()
    shp.fill.fore_color.rgb = fill
    if line is None:
        shp.line.fill.background()
    else:
        shp.line.color.rgb = line
        shp.line.width = Pt(1.25)
    shp.shadow.inherit = False
    if label is not None:
        tf = shp.text_frame
        tf.word_wrap = True
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        tf.margin_left = tf.margin_right = Inches(0.08)
        for i, ln in enumerate(label.split("\n")):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.alignment = align
            r = p.add_run()
            r.text = ln
            r.font.name, r.font.size, r.font.color.rgb = FONT, Pt(size), color
            r.font.bold = bold if i == 0 else False
    return shp


def line(slide, x1, y1, x2, y2, color=LINE, width=1.5, dash=False, arrow=False):
    c = slide.shapes.add_connector(
        MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2)
    )
    c.line.color.rgb = color
    c.line.width = Pt(width)
    if dash:
        c.line.dash_style = 4  # dash
    if arrow:
        ln = c.line._get_or_add_ln()
        tail = ln.makeelement(
            "{http://schemas.openxmlformats.org/drawingml/2006/main}tailEnd",
            {"type": "triangle", "w": "med", "len": "med"},
        )
        ln.append(tail)
    return c


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------


def timeline() -> Presentation:
    prs, s = new_slide()
    text(s, 0.5, 0.35, 12, 0.6, "How one task is recorded", size=28, bold=True)
    text(
        s,
        0.5,
        0.95,
        12,
        0.4,
        "Every task follows the same clock. Only what happens inside the network changes.",
        size=15,
        color=MUTED,
    )

    x0, x1 = 3.1, 12.6  # time axis span in inches
    t_end = 400.0
    fault, alert, before, snap = 120.0, 145.0, 90.0, 360.0

    def X(t):
        return x0 + (x1 - x0) * t / t_end

    # Phase band
    top = 1.5
    box(
        s,
        X(0),
        top,
        X(fault) - X(0),
        0.62,
        GREEN_BG,
        label="Healthy period  ·  ~2 min",
        size=14,
        color=RGBColor(0x06, 0x5F, 0x46),
        bold=True,
    )
    box(
        s,
        X(fault),
        top,
        X(snap) - X(fault),
        0.62,
        RED_BG,
        label="Faulty period  ·  ~4 min",
        size=14,
        color=RGBColor(0x99, 0x1B, 0x1B),
        bold=True,
    )
    box(
        s,
        X(snap),
        top,
        X(t_end) - X(snap),
        0.62,
        GRAY_BG,
        label="Export",
        size=12,
        color=MUTED,
        bold=True,
    )

    lanes = [
        ("Device state", "configs, routing &\nforwarding tables"),
        ("Counters", "interfaces, TCP/IP,\nqueues · every 1 s"),
        ("Logs & flows", "device syslog,\nflow records"),
        ("Network tests", "ping mesh, web & DNS\nchecks · every 20 s"),
        ("Throughput test", "iperf · every 60 s"),
    ]
    lane_y0, lane_h = 2.35, 0.7
    for i, (name, sub) in enumerate(lanes):
        y = lane_y0 + i * lane_h
        line(
            s,
            0.5,
            y + lane_h,
            x1 + 0.2,
            y + lane_h,
            color=RGBColor(0xE5, 0xE7, 0xEB),
            width=0.75,
        )
        text(s, 0.6, y + 0.06, 2.4, 0.3, name, size=14, bold=True)
        text(s, 0.6, y + 0.32, 2.4, 0.4, sub, size=10, color=MUTED)

    def lane_mid(i):
        return lane_y0 + i * lane_h + lane_h / 2

    # Lane 0: two snapshots
    for t, lbl, fill, edge in (
        (before, "before fault", BLUE_BG, BLUE),
        (snap, "final state", BLUE_BG, BLUE),
    ):
        box(
            s,
            X(t) - 0.62,
            lane_mid(0) - 0.24,
            1.24,
            0.48,
            fill,
            line=edge,
            label="snapshot\n" + lbl,
            size=10,
            color=RGBColor(0x1E, 0x3A, 0x8A),
            bold=True,
        )
    # Lane 1: dense ticks (every 5 s drawn, stands for 1 s)
    for t in range(0, int(snap) + 1, 5):
        line(
            s, X(t), lane_mid(1) - 0.2, X(t), lane_mid(1) + 0.2, color=VIOLET, width=1.0
        )
    # Lane 2: continuous stream
    box(s, X(0), lane_mid(2) - 0.1, X(snap) - X(0), 0.2, TEAL_BG, line=TEAL, radius=0.5)
    # Lane 3: probe dots every 20 s
    for t in range(0, int(snap) + 1, 20):
        c = s.shapes.add_shape(
            MSO_SHAPE.OVAL,
            Inches(X(t) - 0.08),
            Inches(lane_mid(3) - 0.08),
            Inches(0.16),
            Inches(0.16),
        )
        c.fill.solid()
        c.fill.fore_color.rgb = AMBER
        c.line.fill.background()
    # Lane 4: iperf every 60 s
    for t in range(0, int(snap) + 1, 60):
        box(
            s,
            X(t),
            lane_mid(4) - 0.13,
            X(3) - X(0) + 0.05,
            0.26,
            RGBColor(0xFD, 0xE6, 0x8A),
            line=AMBER,
            shape=MSO_SHAPE.RECTANGLE,
        )

    lanes_bottom = lane_y0 + len(lanes) * lane_h
    # Event lines
    line(s, X(fault), top, X(fault), lanes_bottom + 0.1, color=RED, width=2.25)
    line(
        s,
        X(alert),
        top + 0.62,
        X(alert),
        lanes_bottom + 0.1,
        color=AMBER,
        width=1.75,
        dash=True,
    )

    # Axis
    ay = lanes_bottom + 0.25
    line(s, X(0), ay, X(t_end), ay, color=MUTED, width=1.25)
    for t, lbl in (
        (0, "0"),
        (60, "1 min"),
        (120, "2 min"),
        (180, "3 min"),
        (240, "4 min"),
        (300, "5 min"),
        (360, "6 min"),
    ):
        line(s, X(t), ay - 0.05, X(t), ay + 0.05, color=MUTED, width=1.25)
        text(
            s,
            X(t) - 0.5,
            ay + 0.08,
            1.0,
            0.25,
            lbl,
            size=10,
            color=MUTED,
            align=PP_ALIGN.CENTER,
        )

    # Callouts under the axis
    cy = ay + 0.4
    box(
        s,
        X(fault) - 1.75,
        cy,
        1.95,
        0.6,
        RED,
        label="Fault introduced\n(none in healthy tasks)",
        size=10,
        color=WHITE,
        bold=True,
    )
    box(
        s,
        X(alert) + 0.15,
        cy,
        1.95,
        0.6,
        AMBER,
        label="Simulated alert\n10–40 s after the fault",
        size=10,
        color=WHITE,
        bold=True,
    )
    text(
        s,
        X(alert) + 2.35,
        cy + 0.06,
        5.6,
        0.6,
        "Healthy tasks get an alert on the same schedule.\nThe exact fault time is only in the labels.",
        size=11,
        color=MUTED,
    )
    return prs


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


def _device(s, x, y, kind):
    """Small schematic device icon centred at (x, y)."""
    if kind == "router":
        c = s.shapes.add_shape(
            MSO_SHAPE.OVAL,
            Inches(x - 0.22),
            Inches(y - 0.15),
            Inches(0.44),
            Inches(0.3),
        )
        c.fill.solid()
        c.fill.fore_color.rgb = BLUE
        c.line.color.rgb = WHITE
        c.line.width = Pt(1.5)
    elif kind == "switch":
        c = s.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE,
            Inches(x - 0.24),
            Inches(y - 0.11),
            Inches(0.48),
            Inches(0.22),
        )
        c.fill.solid()
        c.fill.fore_color.rgb = TEAL
        c.line.fill.background()
    else:
        c = s.shapes.add_shape(
            MSO_SHAPE.RECTANGLE,
            Inches(x - 0.14),
            Inches(y - 0.12),
            Inches(0.28),
            Inches(0.22),
        )
        c.fill.solid()
        c.fill.fore_color.rgb = RGBColor(0x37, 0x41, 0x51)
        c.line.fill.background()
    c.shadow.inherit = False


def overview() -> Presentation:
    prs, s = new_slide()
    text(s, 0.5, 0.35, 12.3, 0.6, "What the dataset contains", size=28, bold=True)
    text(
        s,
        0.5,
        0.95,
        12.3,
        0.4,
        "Each task is one emulated IP network, watched like an operations team would watch it, then saved as files.",
        size=15,
        color=MUTED,
    )

    # Network panel
    box(s, 0.5, 1.7, 3.7, 4.9, GRAY_BG, radius=0.06)
    text(s, 0.7, 1.85, 3.3, 0.35, "1  An emulated network", size=15, bold=True)
    text(
        s,
        0.7,
        2.2,
        3.3,
        0.5,
        "real routing software, real traffic,\none fault introduced (or none)",
        size=11,
        color=MUTED,
    )
    pts = {
        "r1": (1.6, 3.15),
        "r2": (3.1, 3.15),
        "s1": (1.2, 4.2),
        "s2": (2.35, 4.2),
        "s3": (3.5, 4.2),
    }
    for a, b in (("r1", "r2"), ("r1", "s1"), ("r1", "s2"), ("r2", "s2"), ("r2", "s3")):
        line(s, *pts[a], *pts[b], color=RGBColor(0x9C, 0xA3, 0xAF), width=1.75)
    hosts = [
        (0.85, 5.2),
        (1.55, 5.2),
        (2.05, 5.2),
        (2.65, 5.2),
        (3.25, 5.2),
        (3.85, 5.2),
    ]
    for (hx, hy), sw in zip(hosts, ("s1", "s1", "s2", "s2", "s3", "s3")):
        line(s, *pts[sw], hx, hy, color=RGBColor(0x9C, 0xA3, 0xAF), width=1.25)
    for k, (px, py) in pts.items():
        _device(s, px, py, "router" if k.startswith("r") else "switch")
    for hx, hy in hosts:
        _device(s, hx, hy, "host")
    # fault mark
    star = s.shapes.add_shape(
        MSO_SHAPE.LIGHTNING_BOLT, Inches(2.25), Inches(3.35), Inches(0.35), Inches(0.45)
    )
    star.fill.solid()
    star.fill.fore_color.rgb = RED
    star.line.fill.background()
    text(
        s,
        0.7,
        5.75,
        3.3,
        0.7,
        "routers · switches · servers · PCs\n14–24 devices per network",
        size=11,
        color=MUTED,
    )

    # Sources
    text(s, 4.75, 1.85, 4.2, 0.35, "2  Three kinds of evidence", size=15, bold=True)
    srcs = [
        (
            BLUE_BG,
            BLUE,
            "Device state",
            "What you would type on each box:\nconfigs, interfaces, routes, neighbors,\nfirewall rules, protocol sessions, logs",
        ),
        (
            VIOLET_BG,
            VIOLET,
            "Monitoring",
            "What a monitoring system collects:\ncounters every second, syslog,\nflow records (who talks to whom)",
        ),
        (
            RGBColor(0xFE, 0xF3, 0xC7),
            AMBER,
            "Network tests",
            "What an engineer would run:\nping between all PCs, web and DNS\nchecks, throughput tests",
        ),
    ]
    for i, (bg, edge, title, body) in enumerate(srcs):
        y = 2.3 + i * 1.45
        box(s, 4.75, y, 4.1, 1.25, bg, line=edge, radius=0.1)
        text(s, 4.95, y + 0.12, 3.8, 0.3, title, size=14, bold=True, color=edge)
        text(s, 4.95, y + 0.45, 3.8, 0.8, body, size=11)
        line(s, 4.2, 4.15, 4.75, y + 0.62, color=edge, width=1.5, arrow=True)
        line(s, 8.85, y + 0.62, 9.35, 4.15, color=edge, width=1.5, arrow=True)

    # Dataset panel
    box(
        s,
        9.35,
        1.7,
        3.5,
        4.9,
        RGBColor(0xEE, 0xF2, 0xFF),
        line=RGBColor(0xC7, 0xD2, 0xFE),
        radius=0.06,
    )
    text(s, 9.55, 1.85, 3.2, 0.35, "3  One folder per task", size=15, bold=True)
    rows = [
        ("topology.png", "network diagram"),
        ("task.json", "description, devices, links"),
        ("devices/", "state of each device"),
        ("telemetry/", "counters, syslog, flows"),
        ("probes/", "network test results"),
        ("timeline.json", "recording times, alert"),
    ]
    for i, (name, what) in enumerate(rows):
        y = 2.35 + i * 0.52
        box(s, 9.55, y, 3.1, 0.44, WHITE, line=RGBColor(0xC7, 0xD2, 0xFE), radius=0.2)
        text(
            s,
            9.7,
            y + 0.04,
            1.6,
            0.36,
            name,
            size=11,
            bold=True,
            anchor=MSO_ANCHOR.MIDDLE,
        )
        text(
            s,
            11.15,
            y + 0.04,
            1.5,
            0.36,
            what,
            size=9,
            color=MUTED,
            anchor=MSO_ANCHOR.MIDDLE,
        )
    box(
        s,
        9.55,
        5.6,
        3.1,
        0.7,
        RGBColor(0x31, 0x2E, 0x81),
        radius=0.15,
        label="labels.csv  (kept apart)\nfault yes/no · fault type · where",
        size=11,
        color=WHITE,
        bold=True,
    )
    return prs


def render(prs: Presentation, name: str) -> None:
    pptx = HERE / f"{name}.pptx"
    prs.save(pptx)
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        print(f"saved {pptx}; install LibreOffice to render PNG")
        return
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", tmp, str(pptx)],
            check=True,
            capture_output=True,
        )
        pdf = Path(tmp) / f"{name}.pdf"
        subprocess.run(
            [
                "pdftoppm",
                "-png",
                "-r",
                "150",
                "-singlefile",
                str(pdf),
                str(HERE / name),
            ],
            check=True,
        )
    print(f"wrote {HERE / name}.png")


if __name__ == "__main__":
    render(timeline(), "timeline")
    render(overview(), "overview")
