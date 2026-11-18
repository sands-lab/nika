"""Observe a pinned batch until the next report deadline or a verified failure."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    root = args.state.parent
    state = json.loads(args.state.read_text())
    state["watch_pid"] = os.getpid()
    args.state.write_text(json.dumps(state, indent=2))
    remote = Path(state["remote_results"])
    command = [
        "ssh",
        "-o",
        "ConnectTimeout=15",
        state["host"],
        shlex.join(
            [
                "taskset",
                "-c",
                str(state["observer_cpu"]),
                "python3",
                str(remote / "status.py"),
                "--root",
                str(remote / "cpu-aligned-full"),
                "--external",
                str(remote / "cpu-aligned-external-queue"),
            ]
        ),
    ]
    latest = None
    while time.time() < state["next_report_epoch"]:
        try:
            observed = subprocess.run(
                command, capture_output=True, text=True, timeout=45, check=True
            )
            latest = json.loads(observed.stdout)
            external = latest["external"]
            process = external["process"]
            if (
                process.get("live")
                and process.get("start") != state["external_pid_start"]
            ):
                process.update(live=False, identity_mismatch=True)
            data = json.dumps(latest, indent=2)
            temporary = root / "latest.tmp"
            temporary.write_text(data)
            temporary.replace(root / "latest.json")
            (root / f"observation-{int(latest['observed_epoch'])}.json").write_text(
                data
            )
            jobs = external["jobs"]
            external_complete = len(jobs) == state["expected_external_jobs"] and all(
                status == "complete" for status in jobs.values()
            )
            failure = (
                latest["failed_runs"]
                or any(
                    s in {"failed", "interrupted"}
                    for s in latest["mode_receipts"].values()
                )
                or any(s in {"failed", "interrupted"} for s in jobs.values())
                or (
                    not latest["main"]["live"]
                    and latest["completed_runs"] < latest["expected_runs"]
                )
                or (not process["live"] and not external_complete)
            )
            if failure:
                print("ATTENTION", data, flush=True)
                return
            if (
                latest["completed_runs"] == latest["expected_runs"]
                and external_complete
            ):
                print("ALL_COLLECTED", data, flush=True)
                return
        except (subprocess.SubprocessError, ValueError, OSError) as exc:
            with (root / "observation-errors.jsonl").open("a") as output:
                output.write(
                    json.dumps({"epoch": time.time(), "error": str(exc)}) + "\n"
                )
        if (root / "stop").exists():
            print("MONITOR_STOPPED", flush=True)
            return
        time.sleep(min(60, max(0, state["next_report_epoch"] - time.time())))
    print("REPORT_DUE", json.dumps(latest, indent=2), flush=True)


if __name__ == "__main__":
    main()
