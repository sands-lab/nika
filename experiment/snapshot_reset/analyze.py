"""Collect every run into one table of phase times: phases.csv (one row per task, strategy and phase).

Columns: platform, strategy, tier, env, nodes, task, scope, phase, sec
  scope: per_task (paid by every task) | per_variant (once per environment variant per run)
         | build_variant (once per variant and code version) | build_host (once per host)
Every time excludes the agent.
"""

from __future__ import annotations

import csv
import os
import glob
import json
import re
from datetime import datetime
from pathlib import Path

E = Path(os.environ["NIKA_RESET_RESULTS"])
NOB = Path(os.environ["NETOPSBENCH_RUNS"])
FC_ROOT = Path(os.environ["NIKA_FIRECRACKER_ROOT"])

# Phase order is also the colour order in every figure.
PHASES = [
    "Network setup",  # create containers/VMs, boot, start traffic
    "Convergence & readiness",  # wait for routing/services, startup checks, pre-fault baselines
    "Fault injection",
    "Symptom observation",  # benchmark-defined evidence window before the agent
    "Undo & re-verify",  # inverse of the fault, wait for re-convergence, health check
    "Post-recovery baseline",
    "Teardown",
    "Snapshot restore",  # restore VM + attach session (+ fault presence check)
]

# Tier = comparable network size across platforms.
TIERS = {
    ("netopsbench", "xs"): ("S", 6),
    ("netopsbench", "small"): ("S+", 14),
    ("netopsbench", "medium"): ("M", 28),
    ("netopsbench", "large"): ("L", 84),
    ("nika", "dc_clos/s"): ("S", 8),
    ("nika", "dc_clos/m"): ("M", 28),
    ("nika", "dc_clos/l"): ("L", 104),
    ("nika", "k8s_lab"): ("K8s", 20),
    ("netarena", "k8s"): ("K8s", 12),
    ("netarena", "route/2x1"): ("S", 5),
    ("netarena", "route/2x3"): ("S", 9),
    ("netarena", "route/7x3"): ("M", 29),
    ("netarena", "route/8x4"): ("L", 41),
}
rows: list[dict] = []


def add(platform, strategy, env, task, scope, phase, sec):
    tier, nodes = TIERS[(platform, env)]
    assert phase in PHASES, phase
    rows.append(
        dict(
            platform=platform,
            strategy=strategy,
            tier=tier,
            env=env,
            nodes=nodes,
            task=task,
            scope=scope,
            phase=phase,
            sec=round(float(sec), 2),
        )
    )


def ts(s: str) -> datetime:
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.astimezone()


# ---------------------------------------------------------------- NetOpsBench
LOGTS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \| ")


def netopsbench(scale: str, log: Path, run: Path) -> None:
    lines = [
        (ts(m.group(1)), line)
        for line in log.read_text(errors="replace").splitlines()
        if (m := LOGTS.match(line))
    ]

    def first(pat, after=None):
        return next(
            t for t, line in lines if pat in line and (after is None or t >= after)
        )

    deploy = first("[Worker Deploy")
    ready = first("Worker health check passed")
    add(
        "netopsbench",
        "in-place recover",
        scale,
        "-",
        "per_variant",
        "Network setup",
        (ready - deploy).total_seconds(),
    )
    for f in sorted((run / "raw").glob("*/*.json")):
        d = json.loads(f.read_text())
        if not d.get("case_valid"):
            continue
        task, ep, c = d["scenario_id"], d["episode"], d["cleanup"]
        start, inject = ts(d["start_time"]), ts(ep["start_time"])
        obs_end = ts(ep["observations"]["end_time"])
        end = ts(d["end_time"])
        recovered = next(
            (
                t
                for t, line in lines
                if "Recovered from" in line and obs_end <= t <= end
            ),
            None,
        )
        healthy = next(
            (
                t
                for t, line in lines
                if "Worker health check passed" in line
                and (recovered or obs_end) <= t <= end
            ),
            None,
        )
        rebase = c.get("validated_baseline", {}).get("duration_seconds", 0)
        add(
            "netopsbench",
            "in-place recover",
            scale,
            task,
            "per_task",
            "Convergence & readiness",
            (inject - start).total_seconds(),
        )
        add(
            "netopsbench",
            "in-place recover",
            scale,
            task,
            "per_task",
            "Symptom observation",
            (obs_end - inject).total_seconds(),
        )
        if recovered and healthy:
            add(
                "netopsbench",
                "in-place recover",
                scale,
                task,
                "per_task",
                "Undo & re-verify",
                (healthy - obs_end).total_seconds(),
            )
        else:  # healthy cases: nothing to undo, only the health check
            add(
                "netopsbench",
                "in-place recover",
                scale,
                task,
                "per_task",
                "Undo & re-verify",
                c["duration_seconds"] - rebase,
            )
        add(
            "netopsbench",
            "in-place recover",
            scale,
            task,
            "per_task",
            "Post-recovery baseline",
            rebase,
        )
    tail = json.loads(log.read_text().strip().splitlines()[-1])
    end_all = (
        deploy.timestamp() + tail["wall_sec"]
    )  # run_noop starts its timer just before the deploy
    complete = max(t for t, line in lines if "# Scenario Complete" in line)
    add(
        "netopsbench",
        "in-place recover",
        scale,
        "-",
        "per_variant",
        "Teardown",
        end_all - complete.timestamp(),
    )


for scale, run in [("xs", "run-20261027T181751Z")] + [
    (s, None) for s in ("small", "medium", "large")
]:
    log = E / f"logs/netopsbench_{scale}.log"
    if not log.is_file():
        continue
    if run is None:
        m = re.search(r'"run_id": "([^"]+)"', log.read_text())
        if not m:
            continue
        run = m.group(1)
    netopsbench(scale, log, NOB / run)

# ---------------------------------------------------------------- NetArena k8s
for f in sorted(E.glob("netarena/k8s_timing_*.json")):
    d = json.loads(f.read_text())
    i = f.stem.rsplit("_", 1)[1]
    log = (E / "logs/queue.log").read_text()
    m1 = re.search(rf"(\S+) \[ext\] netarena {i} kind create start", log)
    m2 = re.search(rf"(\S+) \[ext\] netarena {i} kind create end", log)
    m3 = re.search(rf"(\S+) \[ext\] netarena {i} k8s end", log)
    m4 = re.search(rf"(\S+) \[ext\] netarena {i} kind deleted", log)
    ot = d["one_time"]
    if m1 and m2:
        add(
            "netarena",
            "partial reset",
            "k8s",
            f"run{i}",
            "per_variant",
            "Network setup",
            (ts(m2.group(1)) - ts(m1.group(1))).total_seconds(),
        )
    add(
        "netarena",
        "partial reset",
        "k8s",
        f"run{i}",
        "per_variant",
        "Convergence & readiness",
        ot.get("cluster_ready_sec", 0)
        + ot["app_deploy_sec"]
        + ot["debug_containers_sec"],
    )
    if m3 and m4:
        add(
            "netarena",
            "partial reset",
            "k8s",
            f"run{i}",
            "per_variant",
            "Teardown",
            (ts(m4.group(1)) - ts(m3.group(1))).total_seconds(),
        )
    for q in d["queries"]:
        t = f"run{i}/q{q['i']}"
        # Re-applying all 12 policies (with the next query's errors) is both the undo and the injection.
        add(
            "netarena",
            "partial reset",
            "k8s",
            t,
            "per_task",
            "Undo & re-verify",
            q["reset_inject_sec"],
        )
        add(
            "netarena",
            "partial reset",
            "k8s",
            t,
            "per_task",
            "Symptom observation",
            q["first_check_sec"],
        )
    for j, s in enumerate(ot.get("clean_app_redeploy_sec", [])):
        add(
            "netarena",
            "redeploy",
            "k8s",
            f"run{i}/r{j}",
            "per_task",
            "Convergence & readiness",
            s,
        )

# NetArena Route: Mininet rebuilt for every query (its own design).
route = E / "netarena/route/route_timing.jsonl"
if route.is_file():
    for i, line in enumerate(route.read_text().splitlines()):
        q = json.loads(line)
        env, t = f"route/{q['num_switches']}x{q['num_hosts_per_subnet']}", f"q{i}"
        add(
            "netarena",
            "rebuild per query",
            env,
            t,
            "per_task",
            "Network setup",
            q["build_sec"],
        )
        add(
            "netarena",
            "rebuild per query",
            env,
            t,
            "per_task",
            "Fault injection",
            q["inject_sec"],
        )
        add(
            "netarena",
            "rebuild per query",
            env,
            t,
            "per_task",
            "Symptom observation",
            q["first_check_sec"],
        )
        add(
            "netarena",
            "rebuild per query",
            env,
            t,
            "per_task",
            "Teardown",
            q["stop_sec"],
        )

# ---------------------------------------------------------------- NIKA


def nika_env(run: dict) -> str:
    size = run.get("scenario_topo_size")
    return f"{run['scenario_name']}/{size}" if size else run["scenario_name"]


def events(trial: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (trial / "nika.jsonl").read_text().splitlines()
        if line.strip()
    ]


def dur(evs, name, origin=None):
    for e in evs:
        if e.get("event") == name and e.get("duration_ms") is not None:
            return e["duration_ms"] / 1000
    return 0.0


def host_ts(evs, name):
    for e in evs:
        if e.get("event") == name and e.get("origin") != "remote":
            return ts(e["timestamp"])
    return None


for strategy, d in [
    ("redeploy", "native"),
    ("snapshot (lab image)", "fc_lab"),
    ("snapshot (case image)", "fc_r1"),
    ("snapshot (case image)", "fc_r2"),
    ("snapshot (case image)", "fc_r3"),
]:
    for trial in sorted(glob.glob(str(E / f"nika/results_{d}/trials/*/"))):
        trial = Path(trial)
        try:
            run = json.loads((trial / "run.json").read_text())
            evs = events(trial)
        except (OSError, json.JSONDecodeError):
            continue
        if not host_ts(evs, "agent_end"):
            continue
        env, task = nika_env(run), f"{d}:{run['trial_id']}"
        fc = run.get("firecracker")
        a = lambda phase, sec: add("nika", strategy, env, task, "per_task", phase, sec)  # noqa: E731
        if fc is None:
            start, verify, workload = (
                dur(evs, "env_start"),
                dur(evs, "env_verify"),
                dur(evs, "env_workload"),
            )
            a("Network setup", start - verify)  # deploy + boot + traffic start
            a("Convergence & readiness", verify)
            a("Fault injection", dur(evs, "failure_inject_complete"))
            a("Teardown", dur(evs, "env_stop"))
        else:
            rebind = host_ts(evs, "remote_session_rebind")
            reverify = dur(
                evs, "env_reverify"
            )  # verification after restore, inside attach_sec
            a(
                "Snapshot restore",
                fc["image_sec"]
                + fc["ready_sec"]
                + max(fc.get("attach_sec", 0) - reverify, 0),
            )
            if fc.get("case"):
                # attach to agent start: the fault presence re-check
                a(
                    "Fault injection",
                    (host_ts(evs, "agent_start") - rebind).total_seconds(),
                )
            else:
                a("Convergence & readiness", reverify)
                a("Fault injection", dur(evs, "failure_inject_complete"))
            stop, end = host_ts(evs, "env_stop"), host_ts(evs, "agent_end")
            a("Teardown", (stop - end).total_seconds() if stop and end else 0.0)

inplace = E / "nika/results_inplace.jsonl"
if inplace.is_file():
    for line in inplace.read_text().splitlines():
        r = json.loads(line)
        env = f"{r['scenario']}/{r['size']}" if r["size"] else r["scenario"]
        if r["kind"] == "setup":
            add(
                "nika",
                "in-place recover",
                env,
                "-",
                "per_variant",
                "Network setup",
                r["env_start_s"] - r.get("env_verify_s", 0),
            )
            add(
                "nika",
                "in-place recover",
                env,
                "-",
                "per_variant",
                "Convergence & readiness",
                r.get("env_verify_s", 0),
            )
        elif r["kind"] == "teardown":
            add(
                "nika",
                "in-place recover",
                env,
                "-",
                "per_variant",
                "Teardown",
                r["teardown_s"],
            )
        elif r.get("ok"):
            t = r["problem"]
            add(
                "nika",
                "in-place recover",
                env,
                t,
                "per_task",
                "Fault injection",
                r.get("inject_s", 0),
            )
            add(
                "nika",
                "in-place recover",
                env,
                t,
                "per_task",
                "Undo & re-verify",
                r.get("recover_s", 0) + r.get("route_restore_s", 0) + r["reverify_s"],
            )
            add(
                "nika",
                "in-place recover (full verify)",
                env,
                t,
                "per_task",
                "Fault injection",
                r.get("inject_s", 0),
            )
            add(
                "nika",
                "in-place recover (full verify)",
                env,
                t,
                "per_task",
                "Undo & re-verify",
                r.get("recover_s", 0)
                + r.get("route_restore_s", 0)
                + r["reverify_s"]
                + r["reverify_full_s"],
            )

# One-time Firecracker builds, from the runner logs.
for log, kind in [("nika_fc_lab.log", "lab"), ("nika_fc_r1.log", "case")]:
    p = E / "logs" / log
    if not p.is_file():
        continue
    text = p.read_text()
    for m in re.finditer(
        r"\[firecracker\] (?:Firecracker|base image) ready in (\d+)s", text
    ):
        rows.append(
            dict(
                platform="nika",
                strategy="snapshot",
                tier="host",
                env="host",
                nodes=0,
                task="base",
                scope="build_host",
                phase="Snapshot restore",
                sec=float(m.group(1)),
            )
        )
    pending = {}
    for line in text.splitlines():
        m = re.search(r"building (lab|case) image (\w+) for (.*)", line)
        if m:
            pending[m.group(2)] = m.group(3)
        m = re.search(r"(lab|case) image (\w+) ready in (\d+)s", line)
        if m and m.group(2) in pending:
            what = pending[m.group(2)]
            case_meta = FC_ROOT / "cases" / m.group(2) / "case.json"
            if (
                m.group(1) == "case" and case_meta.is_file()
            ):  # case image -> its lab image's variant
                lab = json.loads(case_meta.read_text()).get("image")
                lab_meta = FC_ROOT / "images" / str(lab) / "meta.json"
                if lab_meta.is_file():
                    what = str(json.loads(lab_meta.read_text())["variant"])
            env = "k8s_lab" if "k8s_lab" in what else None
            if env is None:
                env = next(
                    (f"dc_clos/{s}" for s in "sml" if f"'topo_size': '{s}'" in what),
                    None,
                )
            rows.append(
                dict(
                    platform="nika",
                    strategy=f"snapshot ({m.group(1)} image)",
                    tier="build",
                    env=env or what,
                    nodes=0,
                    task=m.group(2),
                    scope="build_variant",
                    phase="Snapshot restore",
                    sec=float(m.group(3)),
                )
            )

with open(E / "phases.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
print(f"{len(rows)} rows -> {E / 'phases.csv'}")
