"""Collect per-variant results into one Markdown table.

Reads, for every variant directory, the outputs of ``netkat_equiv`` (both
ECMP semantics) and ``flows``; run those first (``--run`` does it).

    uv run python -m experiment.convergence.report runtime/convergence --run
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

SKIP_PREFIXES = ("symptoms",)  # incident runs, summarized by symptoms_report


def _load(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _classes(doc: dict, mode: str) -> str:
    entry = doc.get(mode)
    if not entry:
        return "-"
    n = len(entry["classes"])
    return f"**{n}**/{entry['runs']}" if n > 1 else f"1/{entry['runs']}"


def _flows(doc: dict, mode: str) -> str:
    rows = doc.get(mode) or []
    if not rows:
        return "-"
    row = max(rows, key=lambda r: r["runs"])
    share = 100 * row["changed_flow_share"]
    text = f"{row['runs_with_changed_flows']}/{row['runs'] - 1} runs, {share:.1f}%"
    return f"**{text}**" if row["runs_with_changed_flows"] else text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--run", action="store_true", help="run the analyses first")
    args = parser.parse_args()
    variants = sorted(
        d
        for d in args.root.iterdir()
        if (d / "native").is_dir() and not d.name.startswith(SKIP_PREFIXES)
    )
    if args.run:
        for d in variants:
            for cmd in (
                ["experiment.convergence.netkat_equiv", str(d), "--ecmp", "set"],
                ["experiment.convergence.netkat_equiv", str(d), "--ecmp", "hash"],
                ["experiment.convergence.flows", str(d)],
            ):
                print("+", " ".join(cmd), file=sys.stderr, flush=True)
                subprocess.run([sys.executable, "-m", *cmd], check=False)
    print(
        "| variant | NetKAT classes, ECMP as set (native) | (Firecracker) "
        "| NetKAT classes, Linux hash ECMP (native) | (Firecracker) "
        "| flows changing next hop vs reference (native) | (Firecracker) |"
    )
    print("|---|---:|---:|---:|---:|---:|---:|")
    for d in variants:
        s, h, f = (
            _load(d / "netkat_set.json"),
            _load(d / "netkat_hash.json"),
            _load(d / "flows.json"),
        )
        if not (s or h or f):
            continue
        print(
            f"| {d.name} | {_classes(s, 'native')} | {_classes(s, 'firecracker')} "
            f"| {_classes(h, 'native')} | {_classes(h, 'firecracker')} "
            f"| {_flows(f, 'native')} | {_flows(f, 'firecracker')} |"
        )


if __name__ == "__main__":
    main()
