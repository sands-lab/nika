"""Where environment start-up time goes, per platform and size: setup.csv and fig4_setup_breakdown.

Sources:
  NIKA        logs/nika_phases.jsonl (probe/nika_phase_probe.py), one start_net_env call per deploy
  NetOpsBench logs/nob_phases_<scale>[_setup].jsonl (probe_nob/sitecustomize.py), the runtime provisioning
  NetArena    netarena/k8s_timing_*.json and logs/queue.log (kind create)
Start-up = from nothing to a converged, verified network with traffic, ready for a fault.
"""

from __future__ import annotations

import csv
import os
import json
import re
import statistics as st
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

E = Path(os.environ["NIKA_RESET_RESULTS"])
L = E / "logs"

# Shared categories (colour order = dataviz reference palette slots 1-7).
CATS = [
    "Config & image prep",
    "Create containers & links",
    "Device boot & wiring",  # SONiC activation, NIKA fault taps, k3s image import
    "Fixed settle / warm-up waits",  # sleeps the frameworks insert so services and traffic settle
    "Convergence & health checks",  # polling until routing / cluster / connectivity checks pass
    "Traffic & telemetry start",
    "App rollout (pull + start pods)",
]
SLOTS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6"]
rows: list[dict] = []
marks: list[dict] = []  # convergence markers, seconds after container creation started


def add(platform, env, nodes, run, cat, sec):
    rows.append(
        dict(
            platform=platform,
            env=env,
            nodes=nodes,
            run=run,
            category=cat,
            sec=round(sec, 2),
        )
    )


# ---------------------------------------------------------------- NIKA
NIKA_NODES = {
    ("dc_clos", "s"): 8,
    ("dc_clos", "m"): 28,
    ("dc_clos", "l"): 104,
    ("k8s_lab", None): 20,
}
p = L / "nika_phases.jsonl"
calls: dict[str, list[dict]] = defaultdict(list)
if p.is_file():
    for line in p.read_text().splitlines():
        r = json.loads(line)
        if r.get("call"):
            calls[r["call"]].append(r)
for call, evs in calls.items():
    by = defaultdict(float)
    first = {}
    for e in evs:
        if "sec" in e and e["sec"] is not None:
            by[e["phase"]] += e["sec"]
            first.setdefault(e["phase"], e)
    if "start_net_env" not in by or not next(
        (e for e in evs if e["phase"] == "start_net_env" and e.get("ok")), None
    ):
        continue
    scen, size = evs[0]["scenario"], evs[0]["size"]
    env = f"{scen}/{size}" if size else scen
    nodes = NIKA_NODES.get((scen, size), 0)
    settle = by["env_deploy"] - by["runtime_deploy"] - by["generate_configs"]
    cats = {
        "Config & image prep": by["generate_configs"] + by["ensure_images"],
        "Create containers & links": by["kathara_deploy_lab"]
        + by["wait_containers_running"],
        "Device boot & wiring": by["record_taps_deploy"]
        if "record_taps_deploy" in by
        else 0.0,
        "Fixed settle / warm-up waits": settle + by["workload_settle_sleep"],
        "Convergence & health checks": by["startup_verify"],
        "Traffic & telemetry start": by["start_workload"]
        - by["workload_settle_sleep"]
        + by["sync_client_hosts"]
        + by["post_deploy"],
        "App rollout (pull + start pods)": by["k8s_image_preload"],
    }
    # record_taps also runs at teardown; count only the calls inside runtime_deploy.
    rd = first["runtime_deploy"]
    taps = sum(
        e["sec"]
        for e in evs
        if e["phase"] == "record_taps"
        and rd["start"] <= e.get("start", 0) <= rd["start"] + rd["sec"]
    )
    cats["Device boot & wiring"] = taps
    covered = sum(cats.values())
    other = by["start_net_env"] - covered
    cats["Config & image prep"] += max(
        other, 0
    )  # session setup, contract generation, small gaps
    for c, v in cats.items():
        add("NIKA", env, nodes, call, c, v)
    conv = next(
        (e for e in evs if e["phase"] == "convergence_readout" and "machines" in e),
        None,
    )
    dl = first["kathara_deploy_lab"]
    if conv:
        t0 = dl["start"]
        m = {"platform": "NIKA", "env": env, "run": call, "total": by["start_net_env"]}
        for key, label in [
            ("containers_last_start", "containers_up"),
            ("bgp_last_up", "bgp_sessions_up"),
            ("bgp_last_route_update", "routes_converged"),
        ]:
            if conv.get(key):
                m[label] = conv[key] - t0
        k8s = conv.get("k8s") or {}
        if (k8s.get("pods") or {}).get("last_ready"):
            m["pods_ready"] = k8s["pods"]["last_ready"] - t0
        m["start_to_deploy"] = dl["start"] - first["start_net_env"]["start"]
        marks.append(m)

# ---------------------------------------------------------------- NetOpsBench
NOB_NODES = {"xs": 6, "small": 14, "medium": 28, "large": 84}
for f in sorted(L.glob("nob_phases_*.jsonl")):
    scale = f.stem.removeprefix("nob_phases_").removesuffix("_setup")
    evs = [json.loads(line) for line in f.read_text().splitlines()]
    dep = next((e for e in evs if e["phase"] == "deploy_worker_lab"), None)
    if dep is None:
        continue
    t_end_dep = dep["start"] + dep["sec"]
    get = lambda name: next((e for e in evs if e["phase"] == name), None)  # noqa: E731
    obs, agent, val = (
        get("ensure_worker_observability"),
        get("ensure_worker_client_agent"),
        get("validate_worker_health"),
    )
    pre_health = next(
        (
            e
            for e in evs
            if e["phase"] == "check_worker_health"
            and agent
            and agent["start"] <= e["start"] <= agent["start"] + agent["sec"]
        ),
        None,
    )
    cats = {
        "Config & image prep": get("generate_topology")["sec"],
        "Create containers & links": get("containerlab_deploy")["sec"],
        "Device boot & wiring": get("apply_configs")["sec"],
        "Fixed settle / warm-up waits": 0.0,
        "Convergence & health checks": (pre_health["sec"] if pre_health else 0)
        + (val["sec"] if val else 0),
        "Traffic & telemetry start": (obs["sec"] if obs else 0)
        + (agent["sec"] - (pre_health["sec"] if pre_health else 0) if agent else 0),
        "App rollout (pull + start pods)": 0.0,
    }
    for c, v in cats.items():
        add("NetOpsBench", scale, NOB_NODES[scale], f.stem, c, v)
    # The first per-case pre-fault baseline (2 x 60 s) is NetOpsBench's own settle requirement;
    # it is per case, so it stays in the per-task figure.

# ---------------------------------------------------------------- NetArena
queue = (L / "queue.log").read_text() if (L / "queue.log").is_file() else ""


def ts(s):
    return datetime.fromisoformat(s)


for f in sorted((E / "netarena").glob("k8s_timing_*.json")):
    i = f.stem.rsplit("_", 1)[1]
    d = json.loads(f.read_text())["one_time"]
    m1 = re.search(rf"(\S+) \[netarena\] {i} kind create start", queue)
    m2 = re.search(rf"(\S+) \[netarena\] {i} kind create end", queue)
    kind = (ts(m2.group(1)) - ts(m1.group(1))).total_seconds() if m1 and m2 else 0.0
    ph = d.get("app_phases") or {}
    cats = {
        "Config & image prep": 0.0,
        "Create containers & links": kind,
        "Device boot & wiring": d.get("debug_containers_sec", 0.0),
        "Fixed settle / warm-up waits": 0.0,
        "Convergence & health checks": d.get("cluster_ready_sec", 0.0),
        "Traffic & telemetry start": 0.0,
        "App rollout (pull + start pods)": ph.get("apply_manifests_sec", 0)
        + ph.get("wait_available_sec", 0)
        + ph.get("delete_app_sec", 0),
    }
    for c, v in cats.items():
        add("NetArena", "k8s", 12, f"run{i}", c, v)

# NetArena Route: Mininet start (namespaces, veth links, OVS bridges, router forwarding).
route = E / "netarena/route/route_timing.jsonl"
if route.is_file():
    for i, line in enumerate(route.read_text().splitlines()):
        q = json.loads(line)
        add(
            "NetArena",
            f"route/{q['num_switches']}x{q['num_hosts_per_subnet']}",
            q["nodes"],
            f"q{i}",
            "Create containers & links",
            q["build_sec"],
        )

with open(E / "setup.csv", "w", newline="") as fh:
    w = csv.DictWriter(
        fh, fieldnames=["platform", "env", "nodes", "run", "category", "sec"]
    )
    w.writeheader()
    w.writerows(rows)

# ---------------------------------------------------------------- summary + figure
groups = defaultdict(lambda: defaultdict(list))
for r in rows:
    groups[(r["platform"], r["env"], r["nodes"])][r["category"]].append(r["sec"])
order = [
    ("NetOpsBench", "xs"),
    ("NetOpsBench", "small"),
    ("NetOpsBench", "medium"),
    ("NetOpsBench", "large"),
    ("NIKA", "dc_clos/s"),
    ("NIKA", "dc_clos/m"),
    ("NIKA", "dc_clos/l"),
    ("NetArena", "route/2x1"),
    ("NetArena", "route/2x3"),
    ("NetArena", "route/7x3"),
    ("NetArena", "route/8x4"),
    ("NetArena", "k8s"),
    ("NIKA", "k8s_lab"),
]
bars = []
lines = [
    "# Environment start-up breakdown (mean seconds per deploy)",
    "",
    "| Platform | Environment | Nodes | Deploys | Total | " + " | ".join(CATS) + " |",
    "|---|---|---:|---:|---:|" + "---:|" * len(CATS),
]
for plat, env in order:
    key = next((k for k in groups if k[0] == plat and k[1] == env), None)
    if key is None:
        continue
    g = groups[key]
    n = len(next(iter(g.values())))
    means = {c: st.mean(g[c]) if g[c] else 0.0 for c in CATS}
    bars.append((f"{plat} {env} ({key[2]} nodes)", means, n))
    lines.append(
        f"| {plat} | {env} | {key[2]} | {n} | {sum(means.values()):.1f} | "
        + " | ".join(f"{means[c]:.1f}" for c in CATS)
        + " |"
    )
lines += [
    "",
    "# Convergence inside NIKA start-up (seconds after container creation began; mean)",
    "",
    "| Environment | Deploys | Containers up | BGP sessions up | Routes converged | Pods ready | start_net_env total |",
    "|---|---:|---:|---:|---:|---:|---:|",
]
mk = defaultdict(list)
for m in marks:
    mk[m["env"]].append(m)
fmt = lambda v: f"{v:.1f}" if v is not None else ""  # noqa: E731
for env, ms in mk.items():

    def avg(k):
        v = [m[k] for m in ms if m.get(k) is not None]
        return st.mean(v) if v else None

    lines.append(
        f"| {env} | {len(ms)} | {fmt(avg('containers_up'))} | {fmt(avg('bgp_sessions_up'))} | {fmt(avg('routes_converged'))} | {fmt(avg('pods_ready'))} | {fmt(avg('total'))} |"
    )
(E / "setup_summary.md").write_text("\n".join(lines) + "\n")
print("\n".join(lines))

if bars:
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
    fig, ax = plt.subplots(figsize=(9.5, 0.45 * len(bars) + 1.8))
    xmax = max(sum(b[1].values()) for b in bars)
    for i, (label, means, n) in enumerate(bars):
        left = 0.0
        for c, col in zip(CATS, SLOTS):
            if means[c] > 0:
                ax.barh(
                    i,
                    means[c],
                    left=left,
                    height=0.62,
                    color=col,
                    edgecolor=SURF,
                    linewidth=1.5,
                )
                left += means[c]
        ax.text(left + xmax * 0.008, i, f"{left:.0f} s", va="center", fontsize=8.5)
    ax.set_yticks(range(len(bars)), [f"{b[0]}  (n={b[2]})" for b in bars])
    ax.invert_yaxis()
    ax.set_xlim(0, xmax * 1.1)
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.tick_params(axis="y", length=0)
    ax.set_xlabel(
        "Mean start-up time per deploy (s): from nothing to a verified network carrying traffic"
    )
    fig.legend(
        [plt.Rectangle((0, 0), 1, 1, color=c) for c in SLOTS],
        CATS,
        loc="upper center",
        ncol=4,
        frameon=False,
        fontsize=8,
        bbox_to_anchor=(0.5, 1.0),
    )
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    (E / "figs").mkdir(exist_ok=True)
    for ext in ("svg", "png"):
        fig.savefig(E / "figs" / f"fig4_setup_breakdown.{ext}", dpi=200)
