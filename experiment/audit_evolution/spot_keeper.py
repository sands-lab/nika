"""Keep a fixed number of spot replicas for this experiment (authorized by the user, 2026-11-15).

Every few minutes, count this experiment's spot deployments that are queued, loading,
or ready; submit plans for any shortfall, replacing preempted or idle-released
replicas. ``--stop`` cancels the deployments this experiment created and exits.
Only the spot profiles listed here are touched.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

KEY = "audit-evolution-20261115"
KG = [
    str(Path.home() / "LocalTools/kaust-gateway/.venv/bin/kaust-gateway"),
    "--config",
    str(Path.home() / ".config/kaust-gateway/config.json"),
]
ACTIVE = {"QUEUED", "PENDING", "LOADING", "READY", "SUBMITTED", "RUNNING"}
GPUS = {"Qwen3.8-27B-FP8-2x80g-sxm-spot": 2, "Qwen3.8-27B-FP8-4x40g-spot": 4}


def gw(*args) -> dict:
    out = subprocess.run(
        ["python3", str(C.GATEWAY_CLI), *args], capture_output=True, text=True
    )
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"error": out.stdout + out.stderr}


def ours(d: dict) -> bool:
    return str(d.get("request_key", "")).startswith(KEY)


def submit(profile: str) -> None:
    plan = gw(
        "plan",
        "--profile",
        profile,
        "--request-key",
        f"{KEY}-{profile}-{int(time.time())}",
    )
    if "id" not in plan:
        print("plan failed:", plan, flush=True)
        return
    subprocess.run(KG + ["approve", plan["id"]], capture_output=True)
    res = gw("apply", plan["id"])
    print(
        time.strftime("%H:%M:%S"),
        "submitted",
        profile,
        res.get("state"),
        res.get("job_id"),
        res.get("error"),
        flush=True,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--target",
        action="append",
        default=[],
        help="PROFILE=N, e.g. Qwen3.8-27B-FP8-2x80g-sxm-spot=3",
    )
    ap.add_argument("--interval", type=int, default=180)
    ap.add_argument(
        "--spot-gpu-cap",
        type=int,
        default=4,
        help="GPUs the gateway allows on QoS spot.",
    )
    ap.add_argument("--stop", action="store_true")
    a = ap.parse_args()
    if a.stop:
        for d in C.deployments():
            if ours(d) and "spot" in str(d.get("profile")) and d.get("state") in ACTIVE:
                print(
                    "cancel",
                    d["id"],
                    d.get("profile"),
                    gw("cancel", d["id"]).get("state"),
                )
        return
    targets = {k: int(v) for k, v in (t.split("=") for t in a.target)}
    while not (C.ROOT / "STOP_SPOT").exists():
        active: dict[str, int] = {}
        for d in C.deployments():
            if ours(d) and d.get("state") in ACTIVE:
                active[d["profile"]] = active.get(d["profile"], 0) + 1
        held = sum(GPUS.get(p, 0) * n for p, n in active.items() if "spot" in p)
        for profile, n in targets.items():
            for _ in range(n - active.get(profile, 0)):
                if held + GPUS[profile] > a.spot_gpu_cap:
                    break  # the gateway would reject it as gpu_cap_reached
                submit(profile)
                held += GPUS[profile]
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
