"""Run the explicit healthy external benchmark plan after NIKA completes."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import time
from pathlib import Path

from experiment.healthy_scenarios.run import require_complete, run_child


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out = args.out.resolve()
    raw = args.plan.read_bytes()
    plan = json.loads(raw)
    args.out.mkdir(parents=True, exist_ok=True)
    print(f"Waiting for the NIKA batch: {args.after}", flush=True)
    while True:
        complete = True
        for variant in plan["nika_variants"]:
            for mode in ("native", "firecracker", "incus"):
                path = args.after / variant / f"{mode}-process.json"
                try:
                    status = json.loads(path.read_text())["status"]
                except (FileNotFoundError, json.JSONDecodeError):
                    complete = False
                    continue
                if status in {"failed", "interrupted"}:
                    raise RuntimeError(f"The NIKA batch failed: {path}")
                complete = complete and status == "complete"
        if complete:
            break
        try:
            process = Path(f"/proc/{plan['nika_pid']}/stat").read_text().split()
        except FileNotFoundError:
            raise RuntimeError(
                "The NIKA process ended without a complete batch"
            ) from None
        if process[2] == "Z" or process[21] != plan["nika_pid_start"]:
            raise RuntimeError("The NIKA process ended without a complete batch")
        time.sleep(10)
    with Path("/tmp/nika-healthy-scenarios.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        require_complete(args.after, ["native", "firecracker", "incus"])
        observed = {path.parent.name for path in args.after.glob("*/params.json")}
        if observed != set(plan["nika_variants"]):
            raise RuntimeError(
                "The preceding NIKA batch does not cover the planned inventory"
            )
        for job in plan["jobs"]:
            path = args.out / f"{job['id']}.json"
            if path.exists():
                raise FileExistsError(path)
            print(job["id"], flush=True)
            env = dict(os.environ, **job.get("env", {}))
            record = {
                "job": job,
                "plan_sha256": hashlib.sha256(raw).hexdigest(),
                "started_epoch": time.time(),
                "status": "running",
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "meminfo_before": Path("/proc/meminfo").read_text(),
            }
            path.write_text(json.dumps(record, indent=2))
            previous_directory = Path.cwd()
            try:
                os.chdir(job["cwd"])
                with (args.out.resolve() / f"{job['id']}.log").open("w") as log:
                    returncode = run_child(job["command"], env, log)
                record.update(
                    returncode=returncode,
                    status="complete" if returncode == 0 else "failed",
                )
            except BaseException:
                record["status"] = "interrupted"
                raise
            finally:
                os.chdir(previous_directory)
                record["finished_epoch"] = time.time()
                path.write_text(json.dumps(record, indent=2))
            if returncode:
                raise SystemExit(f"Inspect the retained failure: {path}")


if __name__ == "__main__":
    main()
