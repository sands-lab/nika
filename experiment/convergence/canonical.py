"""Select the uniform sample: 10 runs per variant and mode, one collector, one kernel boot.

A run qualifies when it was collected by the final collector, which records
the host's ``boot_id`` in ``meta.json``, when every node's dump succeeded,
and, for variants whose FIBs contain
ECMP routes, when its last dump holds per-flow probes from every probed node. The sample is
the first 10 qualifying runs (by run index) of the kernel boot with the most
qualifying runs; Firecracker restores all share their lab image's guest boot.

    uv run python -m experiment.convergence.canonical runtime/convergence           # plan
    uv run python -m experiment.convergence.canonical runtime/convergence --prune   # apply

``--prune`` deletes every run outside the sample and renumbers the sample
``run_01`` .. ``run_10``. It refuses to touch a variant whose sample is short.
"""

from __future__ import annotations

import argparse
import json
import shutil
from collections import defaultdict
from pathlib import Path

from experiment.convergence.parse import failed_nodes, last_dump, parse_json, sections

SAMPLE = 10
MODES = ("native", "firecracker")


def _has_ecmp(doc: dict) -> bool:
    for v in doc["nodes"].values():
        if "raw" not in v:
            continue
        for r in parse_json(sections(v["raw"]).get("route4", "")) or []:
            if r.get("table", "main") == "main" and len(r.get("nexthops") or []) > 1:
                return True
    return False


def _probed(doc: dict) -> bool:
    """Every node that was probed answered (no probe script failed)."""
    probes = [str(v["probes"]) for v in doc["nodes"].values() if "probes" in v]
    return bool(probes) and not any(p.startswith("error") for p in probes)


def plan(variant: Path, mode: str) -> dict:
    runs = []
    ecmp = False
    for run_dir in sorted((variant / mode).glob("run_*")):
        meta_path, dump = run_dir / "meta.json", last_dump(run_dir)
        if not meta_path.exists() or dump is None:
            runs.append((run_dir, None, None))
            continue
        doc = json.loads(dump.read_text())
        ecmp = ecmp or _has_ecmp(doc)
        runs.append((run_dir, json.loads(meta_path.read_text()), doc))
    groups: dict[str, list[Path]] = defaultdict(list)
    for run_dir, meta, doc in runs:
        if meta is None or not meta.get("boot_id") or failed_nodes(doc):
            continue
        if ecmp and not _probed(doc):
            continue
        boot = "lab-image" if mode == "firecracker" else meta["boot_id"]
        groups[boot].append(run_dir)
    boot, chosen = max(groups.items(), key=lambda kv: len(kv[1]), default=(None, []))
    return {
        "runs": [r for r, _, _ in runs],
        "ecmp": ecmp,
        "boot": boot,
        "sample": chosen[:SAMPLE],
        "missing": max(0, SAMPLE - len(chosen)),
    }


def prune(variant: Path, mode: str, p: dict) -> None:
    keep = set(p["sample"])
    for run_dir in p["runs"]:
        if run_dir not in keep:
            shutil.rmtree(run_dir)
    # Two passes so a new name never collides with a run not yet renamed.
    staged = []
    for i, run_dir in enumerate(p["sample"], 1):
        tmp = run_dir.with_name(f".renumber_{i:02d}")
        run_dir.rename(tmp)
        staged.append((tmp, run_dir.with_name(f"run_{i:02d}")))
    for tmp, final in staged:
        tmp.rename(final)
        meta = final / "meta.json"
        doc = json.loads(meta.read_text())
        doc["index"] = int(final.name.split("_")[1])
        meta.write_text(json.dumps(doc, indent=1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--prune", action="store_true")
    args = parser.parse_args()
    short = []
    variants = sorted(
        d
        for d in args.root.iterdir()
        if (d / "native").is_dir()
        and not d.name.startswith(("smoke", "symptoms", "probe"))
    )
    for variant in variants:
        for mode in MODES:
            if not (variant / mode).is_dir():
                continue
            p = plan(variant, mode)
            status = f"missing {p['missing']}" if p["missing"] else "ok"
            print(
                f"{variant.name:34s} {mode:11s} runs={len(p['runs']):2d} ecmp={'Y' if p['ecmp'] else 'n'}"
                f" boot={str(p['boot'])[:8]:9s} sample={len(p['sample']):2d} {status}"
            )
            if p["missing"]:
                short.append((variant.name, mode, p["missing"]))
            elif args.prune:
                prune(variant, mode, p)
    if short:
        print("short:", short)


if __name__ == "__main__":
    main()
