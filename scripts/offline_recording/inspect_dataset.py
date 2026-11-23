"""Check an exported dataset: size per task, failed commands, and file-set leaks.

    uv run python scripts/offline_recording/inspect_dataset.py dataset

Every task of the same network must contain the same files; otherwise the
file list alone could reveal the fault.
"""

from __future__ import annotations

import collections
import csv
import sys
from pathlib import Path

BAD_MARKERS = ("recorder_error", "[TIMEOUT]", "Error executing command", "probe_error")


def main(root: Path) -> int:
    rows = {r["task_id"]: r for r in csv.DictReader((root / "tasks.csv").open())}
    by_network: dict[str, list[tuple[str, set[str]]]] = collections.defaultdict(list)
    for task_id, row in sorted(rows.items()):
        task = root / "tasks" / task_id
        files = [p for p in task.rglob("*") if p.is_file()]
        sizes: collections.Counter[str] = collections.Counter()
        bad = []
        for f in files:
            parts = f.relative_to(task).parts
            key = "/".join(parts[:2]) if parts[0] == "telemetry" else parts[0]
            sizes[key] += f.stat().st_size
            if f.suffix in (".txt", ".log", ".json") and any(
                m in f.read_text(errors="replace") for m in BAD_MARKERS
            ):
                bad.append(str(f.relative_to(task)))
        total = sum(sizes.values())
        print(
            f"{task_id}: {len(files)} files, {total / 1e6:.1f} MB, ~{total / 4e6:.1f} M tokens"
        )
        for key, size in sizes.most_common(4):
            print(f"    {key:28s} ~{size / 4e3:8.0f} k tokens")
        if bad:
            print(f"    files with failed commands: {bad[:10]}")
        by_network[row["network"]].append(
            (task_id, {str(f.relative_to(task)) for f in files})
        )
    leaks = 0
    for network, items in by_network.items():
        ref_id, ref = items[0]
        for task_id, files in items[1:]:
            diff = sorted((ref ^ files) - {"topology.png"})
            if diff:
                leaks += 1
                print(
                    f"FILE-SET DIFFERENCE {network}: {ref_id} vs {task_id}: {diff[:8]}"
                )
    print(
        "file sets identical within each network"
        if not leaks
        else f"{leaks} file-set differences"
    )
    return 1 if leaks else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
