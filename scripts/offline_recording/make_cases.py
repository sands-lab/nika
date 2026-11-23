"""Turn working-pool cases into scheduled recording tasks for the evidence recorder.

Each selected case keeps its deployment and traffic, turns on telemetry, and
moves its failure into a schedule that fires after the baseline window, with
the agent (the recorder) released at t=0. Healthy controls get the same
schedule without events.

    uv run python scripts/offline_recording/make_cases.py \
        dc_clos:host_incorrect_gateway:s campus_lan:healthy:s -o /tmp/rec.yaml
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from nika.agents.recorder.agent import BASELINE_SEC

POOL = Path("benchmark/working/pool")
TELEMETRY = ["prometheus", "syslog", "netflow"]
LICENSED = {"iosxr", "routeros"}
DEPLOY_KEYS = (
    "topo_size",
    "topo",
    "igp",
    "bgp_mode",
    "rpki",
    "backend",
    "device_profile",
)


def _pool_case(scenario: str, failure: str, size: str | None, index: int) -> dict:
    path = POOL / scenario / f"{failure}.yaml"
    doc = yaml.safe_load(path.read_text())
    block = doc.get("failure") or doc.get("healthy") or doc
    cases = (
        (block.get("cases") or block.get("variants"))
        if isinstance(block, dict)
        else None
    )
    if not cases:
        raise SystemExit(f"{path}: no cases")
    # Licensed vendor images (IOS XR, RouterOS) are left out of recordings.
    cases = [c for c in cases if c.get("device_profile") not in LICENSED]
    matches = [
        c for c in cases if size in (None, "") or c.get("topo_size") in (size, None)
    ]
    if not matches:
        sizes = sorted({str(c.get("topo_size")) for c in cases})
        raise SystemExit(f"{path}: no case with topo_size={size} (have {sizes})")
    return matches[min(index, len(matches) - 1)]


def recording_task(
    scenario: str, failure: str, size: str | None, index: int, baseline: int
) -> dict:
    case = _pool_case(scenario, failure, size, index)
    task: dict = {"scenario": scenario}
    task.update({k: case[k] for k in DEPLOY_KEYS if k in case})
    if case.get("traffic"):
        task["traffic"] = case["traffic"]
    task["telemetry"] = list(TELEMETRY)
    events = []
    if failure != "healthy":
        events.append(
            {
                "problem": failure,
                "inject": dict(case.get("inject") or {}),
                "at": f"{baseline}s",
            }
        )
    task["schedule"] = {"agent_start": "0s", "events": events}
    return task


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("selectors", nargs="+", help="scenario:failure[:size[:index]]")
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("--baseline", type=int, default=BASELINE_SEC)
    args = parser.parse_args()
    tasks = []
    for sel in args.selectors:
        parts = sel.split(":")
        scenario, failure = parts[0], parts[1]
        size = parts[2] if len(parts) > 2 else None
        index = int(parts[3]) if len(parts) > 3 else 0
        tasks.append(recording_task(scenario, failure, size, index, args.baseline))
    Path(args.output).write_text(yaml.safe_dump({"cases": tasks}, sort_keys=False))
    print(f"wrote {len(tasks)} recording tasks to {args.output}")


if __name__ == "__main__":
    main()
