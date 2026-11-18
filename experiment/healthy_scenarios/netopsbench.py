"""Measure NetOpsBench's healthy runtime lifecycle without creating incidents.

The shared observability core is a prerequisite. Worker pools use the upstream
SDK lifecycle and teardown. Run sequentially with the NIKA measurements.
"""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

UPSTREAM = "d121e55b51f317b9f6bc684be0745184457740b8"


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--scale", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    commit = subprocess.check_output(
        ["git", "-C", str(args.checkout), "rev-parse", "HEAD"], text=True
    ).strip()
    if commit != UPSTREAM:
        parser.error(f"Expected upstream {UPSTREAM}, found {commit}")
    sys.path.insert(0, str(args.checkout.resolve()))
    from netopsbench.sdk.runtimes import RuntimeManager
    from netopsbench.platform.observability.lifecycle import ensure_observability_core
    from experiment.healthy_scenarios.sample import SCRIPT, sample

    args.out.mkdir(parents=True)
    started = time.monotonic()
    ensure_observability_core()
    (args.out / "shared-core.json").write_text(
        json.dumps(
            {
                "phase": "shared_observability_prerequisite",
                "prepare_sec": time.monotonic() - started,
                "upstream": commit,
            },
            indent=2,
        )
    )
    for index in range(1, 4):
        root = args.out / f"run_{index:02d}"
        root.mkdir()
        record = {
            "upstream": commit,
            "scale": args.scale,
            "healthy_only": True,
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "phases": [],
            "started_epoch": time.time(),
        }

        def timed(phase, function):
            started, epoch = time.monotonic_ns(), time.time_ns()
            try:
                return function()
            finally:
                record["phases"].append(
                    {
                        "phase": phase,
                        "start_epoch_ns": epoch,
                        "duration_ns": time.monotonic_ns() - started,
                    }
                )

        pool = None
        try:
            manager = RuntimeManager(workspace=root)
            pool = manager.create(scale=args.scale, workers=1)
            record["runtime_id"] = pool.id
            timed("deployment", pool.deploy)
            timed("observability", pool.ensure_observability)
            timed("pingmesh", pool.ensure_pingmesh)
            timed("health_verification", pool.warm)
            ready = time.monotonic()

            def export():
                containers = []
                for worker in pool.workers:
                    output = subprocess.check_output(
                        [
                            "docker",
                            "ps",
                            "--filter",
                            f"label=containerlab={worker.lab_name}",
                            "--format",
                            "{{.ID}} {{.Names}}",
                        ],
                        text=True,
                    )
                    containers.extend(line.split() for line in output.splitlines())
                if not containers:
                    raise RuntimeError("No containers found for the owned runtime")

                def one(container):
                    identifier, name = container
                    raw = subprocess.check_output(
                        ["docker", "exec", identifier, "sh", "-c", SCRIPT],
                        text=True,
                        timeout=300,
                    )
                    return name, {"raw": raw}

                with ThreadPoolExecutor(8) as executor:
                    return dict(executor.map(one, containers))

            sample(root, ready, export)
            record["upstream_stages"] = {
                str(key): value.model_dump(mode="json")
                if hasattr(value, "model_dump")
                else value
                for key, value in pool.stage_results.items()
            }
            record["status"] = "complete"
        except BaseException:
            record.update(status="failed", error=traceback.format_exc())
            raise
        finally:
            try:
                if pool is not None:
                    timed("teardown", pool.teardown)
            except BaseException:
                record["cleanup_error"] = traceback.format_exc()
                raise
            finally:
                (root / "result.json").write_text(
                    json.dumps(record, indent=2, default=str)
                )


if __name__ == "__main__":
    main()
