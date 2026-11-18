"""Summarize retained healthy measurements without changing the running batch."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import defaultdict
from pathlib import Path


def read(path: Path) -> dict:
    return json.loads(path.read_text())


def process(pid: int, start: str | None = None) -> dict:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
    except FileNotFoundError:
        return {"pid": pid, "live": False}
    return {
        "pid": pid,
        "live": fields[0] != "Z" and (start is None or fields[19] == start),
        "state": fields[0],
        "start": fields[19],
        "command": command.decode(errors="replace").strip(),
    }


def summarize(root: Path) -> dict:
    launch = read(root / "launch.json")
    command = launch["command"]
    manifest = read(Path(command[command.index("--manifest") + 1]))
    groups = defaultdict(list)
    for path in root.glob("*/*/run_*/meta.json"):
        record = read(path)
        if not all((path.parent / f"dump_{delay}.json").exists() for delay in (0, 60)):
            raise RuntimeError(f"Completed metadata lacks state exports: {path}")
        groups[(path.parents[2].name, record["mode"])].append(record)
    results = []
    for (variant, mode), records in sorted(groups.items()):
        records.sort(key=lambda record: record["index"])
        key = "deploy_sec" if mode == "native" else "prepare_sec"
        values = [record["timings"][key] for record in records]
        timing_keys = sorted(
            {
                name
                for record in records
                for name, value in record["timings"].items()
                if isinstance(value, (int, float))
            }
        )
        components = {}
        for name in timing_keys:
            samples = [
                record["timings"][name]
                for record in records
                if name in record["timings"]
            ]
            components[name] = {
                "values": samples,
                "median": statistics.median(samples),
            }
        build_path = root / variant / f"{mode}-build.json"
        results.append(
            {
                "variant": variant,
                "scenario": records[0]["scenario"],
                "mode": mode,
                "completed": len(records),
                "ready_seconds": values,
                "median_ready_seconds": statistics.median(values),
                "timing_components_seconds": components,
                "snapshot_preparation_seconds": read(build_path)["prepare_sec"]
                if build_path.exists()
                else None,
            }
        )
    errors = [
        {"path": str(path), "error": read(path).get("error", "")[-3000:]}
        for path in sorted(root.glob("*/*/run_*/error.json"))
    ]
    receipts = {
        str(path.relative_to(root)): read(path)["status"]
        for path in sorted(root.glob("*/*-process.json"))
    }
    children = []
    for path in Path("/proc").glob("[0-9]*"):
        try:
            fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[1]) == launch["pid"]:
                children.append(process(int(path.name)))
        except (FileNotFoundError, ProcessLookupError):
            continue
    return {
        "observed_epoch": time.time(),
        "main": process(launch["pid"], launch["pid_start"]),
        "children": children,
        "expected_runs": len(manifest["variants"])
        * len(manifest["modes"])
        * manifest["runs_per_mode"],
        "completed_runs": sum(len(records) for records in groups.values()),
        "failed_runs": errors,
        "mode_receipts": receipts,
        "results": results,
        "batch_tail": (root / "batch.log").read_text()[-2000:],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--external", type=Path)
    args = parser.parse_args()
    result = summarize(args.root)
    if args.external:
        launch = read(args.external / "launch.json")
        result["external"] = {
            "process": process(launch["pid"]),
            "jobs": {
                path.stem: read(path).get("status")
                for path in sorted(args.external.glob("*.json"))
                if path.name != "launch.json"
            },
            "log_tail": (args.external / "batch.log").read_text()[-2000:],
        }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
