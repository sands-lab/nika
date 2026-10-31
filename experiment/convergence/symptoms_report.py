"""Summarize ``symptoms`` runs: how many distinct symptom sets one failure produced.

A run's symptom set is the directed host pairs whose loss after injection is
at least ``--threshold`` while it was zero before injection.

    uv run python -m experiment.convergence.symptoms_report runtime/convergence/symptoms_dc_clos_m
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def symptom_set(doc: dict, threshold: float) -> frozenset[str]:
    """Pairs that lose at least ``threshold`` after injection and nothing before."""
    return frozenset(
        k
        for k, v in doc["after"].items()
        if v is not None and v >= threshold and doc["before"].get(k) == 0
    )


def missing_pairs(doc: dict) -> int:
    """Pairs without a measurement (the pingmesh command timed out)."""
    return sum(v is None for part in ("before", "after") for v in doc[part].values())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--threshold", type=float, default=0.1)
    args = parser.parse_args()
    for mode in ("native", "firecracker"):
        runs = {
            p.parent.name: json.loads(p.read_text())
            for p in sorted((args.root / mode).glob("run_*/pingmesh.json"))
            if (p.parent / "meta.json").exists()
        }
        if not runs:
            continue
        noisy = {
            r: sum((v or 0) > 0 for v in d["before"].values()) for r, d in runs.items()
        }
        missing = sum(missing_pairs(d) for d in runs.values())
        sets = {r: symptom_set(d, args.threshold) for r, d in runs.items()}
        classes = Counter(sets.values())
        ref = sets[min(sets)]
        jaccard = [
            len(ref & s) / len(ref | s) if ref | s else 1.0
            for r, s in sets.items()
            if r != min(sets)
        ]
        print(f"=== {args.root.name} [{mode}] {len(runs)} runs")
        print(f"    pairs with loss before injection: {sorted(noisy.values())}")
        if missing:
            print(f"    pairs without a measurement (timed out): {missing}")
        print(
            f"    distinct symptom sets: {len(classes)}; sizes {sorted(len(s) for s in sets.values())}"
        )
        print(
            f"    Jaccard vs {min(sets)}: min {min(jaccard, default=1):.2f}, mean {sum(jaccard) / max(len(jaccard), 1):.2f}"
        )
        for s, n in classes.most_common():
            print(f"      {n}x {sorted(s)}")


if __name__ == "__main__":
    main()
