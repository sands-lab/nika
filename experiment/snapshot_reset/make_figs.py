"""Figures and summary tables from phases.csv (run analyze.py first).

fig1_breakdown   per-task environment time, by phase, one panel per network size tier
fig2_setup       one-time network build per environment vs. snapshot build cost
fig3_repeat      cumulative environment time of the NIKA 36-task suite over repeated runs
summary.md       the numbers behind every bar and line
"""

from __future__ import annotations

import csv
import os
import statistics as st
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

E = Path(os.environ["NIKA_RESET_RESULTS"])
OUT = E / "figs"
OUT.mkdir(exist_ok=True)

PHASES = [
    "Network setup",
    "Convergence & readiness",
    "Fault injection",
    "Symptom observation",
    "Undo & re-verify",
    "Post-recovery baseline",
    "Teardown",
    "Snapshot restore",
]
# Validated reference palette (dataviz), light mode, slots 1-8 in order: colour = phase.
SLOTS = [
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#6250d6",
    "#e34948",
]
COLOR = dict(zip(PHASES, SLOTS))
INK, INK2, GRID, SURF = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 9,
        "axes.edgecolor": GRID,
        "axes.labelcolor": INK2,
        "xtick.color": INK2,
        "ytick.color": INK2,
        "text.color": INK,
        "figure.facecolor": SURF,
        "axes.facecolor": SURF,
        "svg.fonttype": "none",
        "axes.spines.top": False,
        "axes.spines.right": False,
    }
)

rows = list(csv.DictReader(open(E / "phases.csv")))
SETUP = (
    list(csv.DictReader(open(E / "setup.csv"))) if (E / "setup.csv").is_file() else []
)
for r in rows:
    r["sec"] = float(r["sec"])


def per_task(platform, strategy, env):
    """Mean seconds per phase over the tasks of one (platform, strategy, env), and the task count."""
    by_task = defaultdict(lambda: defaultdict(float))
    for r in rows:
        if (r["platform"], r["strategy"], r["env"], r["scope"]) == (
            platform,
            strategy,
            env,
            "per_task",
        ):
            by_task[r["task"]][r["phase"]] += r["sec"]
    n = len(by_task)
    if not n:
        return {}, 0, []
    means = {p: sum(t.get(p, 0) for t in by_task.values()) / n for p in PHASES}
    totals = [sum(t.values()) for t in by_task.values()]
    return means, n, totals


def once(platform, strategy, env, scope="per_variant"):
    """Mean over runs of the summed one-time phases of one variant (e.g. deploy + teardown)."""
    by_task = defaultdict(lambda: defaultdict(float))
    for r in rows:
        if (r["platform"], r["strategy"], r["env"], r["scope"]) == (
            platform,
            strategy,
            env,
            scope,
        ):
            by_task[r["task"]][r["phase"]] += r["sec"]
    if not by_task:
        return {}
    return {p: st.mean(t.get(p, 0) for t in by_task.values()) for p in PHASES}


NIKA_STRATEGIES = [
    ("redeploy", "redeploy"),
    ("in-place recover", "in-place recover"),
    ("snapshot (lab image)", "snapshot, lab image"),
    ("snapshot (case image)", "snapshot, case image"),
]
TIERS = [
    (
        "Small tier (5-14 nodes)",
        [
            (
                "netopsbench",
                "in-place recover",
                "xs",
                "NetOpsBench XS (6 nodes) · in-place",
            ),
            (
                "netopsbench",
                "in-place recover",
                "small",
                "NetOpsBench Small (14) · in-place",
            ),
            (
                "netarena",
                "rebuild per query",
                "route/2x1",
                "NetArena Route 2x1 (5) · rebuild",
            ),
            (
                "netarena",
                "rebuild per query",
                "route/2x3",
                "NetArena Route 2x3 (9) · rebuild",
            ),
        ]
        + [
            ("nika", s, "dc_clos/s", f"NIKA dc_clos s (8) · {lbl}")
            for s, lbl in NIKA_STRATEGIES
        ],
    ),
    (
        "Medium tier (28-29 nodes)",
        [
            (
                "netopsbench",
                "in-place recover",
                "medium",
                "NetOpsBench Medium (28) · in-place",
            ),
            (
                "netarena",
                "rebuild per query",
                "route/7x3",
                "NetArena Route 7x3 (29) · rebuild",
            ),
        ]
        + [
            ("nika", s, "dc_clos/m", f"NIKA dc_clos m (28) · {lbl}")
            for s, lbl in NIKA_STRATEGIES
        ],
    ),
    (
        "Large tier (41-104 nodes)",
        [
            (
                "netopsbench",
                "in-place recover",
                "large",
                "NetOpsBench Large (84) · in-place",
            ),
            (
                "netarena",
                "rebuild per query",
                "route/8x4",
                "NetArena Route 8x4 (41, max) · rebuild",
            ),
        ]
        + [
            ("nika", s, "dc_clos/l", f"NIKA dc_clos l (104) · {lbl}")
            for s, lbl in NIKA_STRATEGIES
        ],
    ),
    (
        "Kubernetes tier",
        [
            (
                "netarena",
                "partial reset",
                "k8s",
                "NetArena k8s (12 svc) · partial reset",
            ),
            ("netarena", "redeploy", "k8s", "NetArena k8s · redeploy app"),
        ]
        + [
            ("nika", s, "k8s_lab", f"NIKA k8s_lab (20) · {lbl}")
            for s, lbl in NIKA_STRATEGIES
        ],
    ),
]

summary = [
    "# Per-task environment time (seconds, agent excluded)",
    "",
    "| Tier | Platform · strategy | Tasks | Mean | Median | "
    + " | ".join(PHASES)
    + " |",
    "|---|---|---:|---:|---:|" + "---:|" * len(PHASES),
]

# ---------------------------------------------------------------- Figure 1
fig, axes = plt.subplots(len(TIERS), 1, figsize=(10, 11.5), sharex=True)
xmax = 0
panels = []
for title, entries in TIERS:
    bars = []
    for platform, strategy, env, label in entries:
        means, n, totals = per_task(platform, strategy, env)
        if n:
            bars.append((label, means, n, totals))
            summary.append(
                f"| {title.split(':')[0]} | {label} | {n} | {st.mean(totals):.1f} | {st.median(totals):.1f} | "
                + " | ".join(f"{means[p]:.1f}" if means[p] else "" for p in PHASES)
                + " |"
            )
            xmax = max(xmax, sum(means.values()))
    panels.append((title, bars))
for ax, (title, bars) in zip(axes, panels):
    for i, (label, means, n, totals) in enumerate(bars):
        left = 0.0
        for p in PHASES:
            if means[p] > 0:
                ax.barh(
                    i,
                    means[p],
                    left=left,
                    height=0.62,
                    color=COLOR[p],
                    edgecolor=SURF,
                    linewidth=1.5,
                )
                left += means[p]
        ax.text(
            left + xmax * 0.008,
            i,
            f"{left:.0f} s" if left >= 10 else f"{left:.1f} s",
            va="center",
            ha="left",
            fontsize=8.5,
            color=INK,
        )
    ax.set_yticks(range(len(bars)), [f"{b[0]}  (n={b[2]})" for b in bars])
    ax.invert_yaxis()
    ax.set_title(title, loc="left", fontsize=9.5, color=INK, pad=4)
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.tick_params(axis="y", length=0)
axes[-1].set_xlim(0, xmax * 1.12)
axes[-1].set_xlabel(
    "Mean environment time per task (s); the agent's own time is excluded"
)
handles = [plt.Rectangle((0, 0), 1, 1, color=COLOR[p]) for p in PHASES]
fig.legend(
    handles,
    PHASES,
    loc="upper center",
    ncol=4,
    frameon=False,
    fontsize=8.5,
    bbox_to_anchor=(0.5, 1.0),
)
fig.tight_layout(rect=(0, 0, 1, 0.95))
for ext in ("svg", "png"):
    fig.savefig(OUT / f"fig1_breakdown.{ext}", dpi=200)
plt.close(fig)

# ---------------------------------------------------------------- Figure 2
setup_rows = []
for platform, strategy, env, label in [
    ("netopsbench", "in-place recover", "xs", "NetOpsBench XS"),
    ("netopsbench", "in-place recover", "small", "NetOpsBench Small"),
    ("netopsbench", "in-place recover", "medium", "NetOpsBench Medium"),
    ("netopsbench", "in-place recover", "large", "NetOpsBench Large"),
    ("netarena", "partial reset", "k8s", "NetArena k8s"),
    ("nika", "in-place recover", "dc_clos/s", "NIKA dc_clos s"),
    ("nika", "in-place recover", "dc_clos/m", "NIKA dc_clos m"),
    ("nika", "in-place recover", "dc_clos/l", "NIKA dc_clos l"),
    ("nika", "in-place recover", "k8s_lab", "NIKA k8s_lab"),
]:
    setup_env = {
        "xs": "xs",
        "small": "small",
        "medium": "medium",
        "large": "large",
    }.get(env, env)
    tot = defaultdict(float)
    for r in SETUP:
        if r["platform"].lower() == platform and r["env"] == setup_env:
            tot[r["run"]] += float(r["sec"])
    if not tot:
        continue
    build = st.mean(tot.values())
    lab = [
        r["sec"]
        for r in rows
        if r["scope"] == "build_variant"
        and r["strategy"] == "snapshot (lab image)"
        and r["env"] == env
    ]
    case = [
        r["sec"]
        for r in rows
        if r["scope"] == "build_variant"
        and r["strategy"] == "snapshot (case image)"
        and r["env"] == env
    ]
    setup_rows.append(
        (
            label,
            build,
            st.mean(lab) if lab else None,
            st.mean(case) if case else None,
            len(case),
        )
    )
summary += [
    "",
    "# One-time network build and snapshot builds (seconds)",
    "",
    "| Environment | Start-up to a verified network (instrumented, once per run) | Lab image build (once per code version) | Case image build, mean per task (n) |",
    "|---|---:|---:|---:|",
]
for label, build, lab, case, n in setup_rows:
    summary.append(
        f"| {label} | {build:.0f} | {'' if lab is None else f'{lab:.0f}'} | {'' if case is None else f'{case:.0f} ({n})'} |"
    )
host = [r["sec"] for r in rows if r["scope"] == "build_host"]
summary.append(
    f"\nHost one-time (Firecracker build + base image): {sum(host):.0f} s"
    if host
    else "\nHost one-time: base image was current (not rebuilt in this run)"
)

S2 = {
    "Start-up (deploy to verified network)": SLOTS[0],
    "Lab image build": SLOTS[1],
    "Case image build (per task)": SLOTS[2],
}
fig_rows = [r for r in setup_rows if r[2] is not None]
fig, ax = plt.subplots(figsize=(8.5, 3.8))
h = 0.26
for i, (label, build, lab, case, n) in enumerate(fig_rows):
    for j, (name, val) in enumerate(zip(S2, (build, lab, case))):
        if val is None:
            continue
        y = i + (j - 1) * h
        ax.barh(y, val, height=h * 0.92, color=S2[name], edgecolor=SURF, linewidth=1)
        ax.text(val + 4, y, f"{val:.0f}", va="center", fontsize=7.5, color=INK2)
ax.set_yticks(range(len(fig_rows)), [r[0] for r in fig_rows])
ax.invert_yaxis()
ax.grid(axis="x", color=GRID, linewidth=0.6)
ax.set_axisbelow(True)
ax.tick_params(axis="y", length=0)
ax.set_xlabel("Seconds")
fig.legend(
    [plt.Rectangle((0, 0), 1, 1, color=c) for c in S2.values()],
    list(S2),
    frameon=False,
    fontsize=8,
    loc="upper center",
    ncol=3,
    bbox_to_anchor=(0.5, 1.0),
)
fig.tight_layout(rect=(0, 0, 1, 0.92))
for ext in ("svg", "png"):
    fig.savefig(OUT / f"fig2_setup.{ext}", dpi=200)
plt.close(fig)

# ---------------------------------------------------------------- Figure 3
# NIKA 36-task suite, one trial per task per run. One-time costs at run 0.
envs = ["dc_clos/s", "dc_clos/m", "dc_clos/l", "k8s_lab"]
per_run, upfront = {}, {}
TASKS_PER_ENV = {"dc_clos/s": 10, "dc_clos/m": 10, "dc_clos/l": 10, "k8s_lab": 6}
for strategy, label in NIKA_STRATEGIES:
    task_sum = 0.0
    for env in envs:
        means, n, totals = per_task("nika", strategy, env)
        # mean over every measured trial (several rounds for case images) x tasks in one run
        task_sum += st.mean(totals) * TASKS_PER_ENV[env] if n else 0
    per_run[label] = task_sum
    upfront[label] = 0.0
inplace_variant = sum(
    sum(once("nika", "in-place recover", env).values())
    for env in envs
    if once("nika", "in-place recover", env)
)
per_run["in-place recover"] += (
    inplace_variant  # each run deploys and tears down every variant once
)
host_build = (
    sum(host)
    if host
    else float(open(E / "host_build_sec.txt").read())
    if (E / "host_build_sec.txt").is_file()
    else 0.0
)
lab_build = sum(
    r["sec"]
    for r in rows
    if r["scope"] == "build_variant" and r["strategy"] == "snapshot (lab image)"
)
case_build = sum(
    r["sec"]
    for r in rows
    if r["scope"] == "build_variant" and r["strategy"] == "snapshot (case image)"
)
upfront["snapshot, lab image"] = host_build + lab_build
upfront["snapshot, case image"] = host_build + lab_build + case_build
LINE = dict(zip([lbl for _, lbl in NIKA_STRATEGIES], SLOTS[:4]))
runs = list(range(0, 11))
fig, ax = plt.subplots(figsize=(7.5, 4.4))
ends = {}
for label in per_run:
    ys = [(upfront[label] + k * per_run[label]) / 3600 for k in runs]
    ax.plot(runs, ys, color=LINE[label], linewidth=2, marker="o", markersize=4)
    ends[label] = ys[-1]
placed = []
gap = max(ends.values()) * 0.045
for label, y in sorted(ends.items(), key=lambda kv: kv[1]):
    y_text = max([y] + [p + gap for p in placed])
    placed.append(y_text)
    ax.text(
        runs[-1] + 0.15,
        y_text,
        f"{label} ({ends[label]:.1f} h)",
        va="center",
        fontsize=8.5,
        color=INK,
    )
ax.set_xlim(0, 13.4)
ax.set_xticks(runs)
ax.set_xlabel("Runs of the 36-task suite (one trial per task)")
ax.set_ylabel("Cumulative environment time (h)")
ax.grid(color=GRID, linewidth=0.6)
ax.set_axisbelow(True)
ax.set_title(
    "NIKA: repeated runs, one-time builds included at run 0", loc="left", fontsize=9.5
)
fig.tight_layout()
for ext in ("svg", "png"):
    fig.savefig(OUT / f"fig3_repeat.{ext}", dpi=200)
plt.close(fig)

summary += [
    "",
    "# Repeated runs of the NIKA 36-task suite (seconds)",
    "",
    "| Strategy | One-time | Per run | Break-even vs redeploy (runs) | Break-even vs in-place (runs) |",
    "|---|---:|---:|---:|---:|",
]
for label in per_run:
    gain = per_run["redeploy"] - per_run[label]
    be = upfront[label] / gain if gain > 0 and upfront[label] else 0
    gain2 = per_run["in-place recover"] - per_run[label]
    be2 = upfront[label] / gain2 if gain2 > 0 and upfront[label] else 0
    summary.append(
        f"| {label} | {upfront[label]:.0f} | {per_run[label]:.0f} | {be:.2f} | {be2:.2f} |"
    )
summary.append(
    f"\nHost build: {host_build:.0f} s, lab images: {lab_build:.0f} s, case images: {case_build:.0f} s; "
    f"in-place per-run deploy+teardown of the 4 variants: {inplace_variant:.0f} s"
)
(E / "summary.md").write_text("\n".join(summary) + "\n")
print("\n".join(summary))
