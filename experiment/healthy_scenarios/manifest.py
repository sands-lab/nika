"""Export healthy deployment variants from one Test task matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import yaml

from nika.config import REPO_ROOT
from nika.firecracker.image import variant_for_row, vm_cpus, vm_memory_mib


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    raw = args.cases.read_bytes()
    rows = yaml.safe_load(raw)["cases"]
    variants = {}
    for index, row in enumerate(rows):
        params = variant_for_row(row)
        identity = hashlib.sha256(
            json.dumps(params, sort_keys=True).encode()
        ).hexdigest()[:16]
        if identity not in variants:
            variants[identity] = {
                "id": identity,
                "params": params,
                "vm_memory_mib": vm_memory_mib(params),
                "vm_cpus": vm_cpus(params),
                "rows": [],
            }
        variants[identity]["rows"].append({"index": index, "slice": row.get("slice")})
    commit = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    output = {
        "source_root": str(REPO_ROOT),
        "source_commit": commit.stdout.strip() or None,
        "matrix": str(args.cases.resolve()),
        "matrix_sha256": hashlib.sha256(raw).hexdigest(),
        "tasks": len(rows),
        "scenario_count": len({row["scenario"] for row in rows}),
        "healthy_only": True,
        "runs_per_mode": 3,
        "modes": ["native", "firecracker", "incus"],
        "delays_sec": [0, 60],
        "variants": list(variants.values()),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as stream:
        json.dump(output, stream, indent=2)
    print(
        f"{len(rows)} tasks, {output['scenario_count']} scenarios, {len(variants)} variants"
    )


if __name__ == "__main__":
    main()
