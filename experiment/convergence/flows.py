"""Per-flow ECMP decisions measured with ``ip route get`` across runs.

Each probe asks a router's kernel which next hop it uses for one
(source, destination) flow through an ECMP route. Runs are compared only
within one kernel boot (``meta.json`` ``boot_id``), because Linux keys its
multipath hash once per boot; Firecracker copies all restore one guest kernel.

Two numbers per variant:

* flows that take a different next hop than in the reference run, which is
  the data-plane difference a ``traceroute`` or an elephant flow sees;
* position agreement: the share of flows that take the same position in the
  route's nexthop list in both runs. Linux hash-threshold selection makes it
  100% when the hash key is unchanged, which validates the ``--ecmp hash``
  NetKAT model against the real kernel.

    uv run python -m experiment.convergence.flows runtime/convergence/dc_clos_m
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

from experiment.convergence.parse import (
    gateway,
    last_dump,
    link_local_owners,
    parse_json,
    sections,
)

_VIA = re.compile(r"\bvia (?:inet6 )?(\S+) dev (\S+)")
_DEV = re.compile(r"\bdev (\S+)")


def _hop(answer: str, owners: dict[str, str]) -> str:
    """The forwarding decision in one ``ip route get`` answer."""
    if m := _VIA.search(answer):
        return f"{owners.get(m.group(1), m.group(1))}@{m.group(2)}"
    if answer.startswith("local "):
        return "local"
    if m := _DEV.search(answer):
        return f"on-link@{m.group(1)}"
    return f"unresolved: {answer.strip()[:60]}"


def probes(dump: dict) -> dict[tuple, tuple[str, int]]:
    """(node, prefix, src, dst) -> (decision, position in the ECMP nexthop list).

    The decision is ``gw@dev``, ``on-link@dev``, ``local`` or the kernel's
    error, so a flow that resolves in one run and not in another counts as
    changed. The position is -1 when the decision is not an ECMP member.
    """
    out = {}
    # IPv6 link-local next hops derive from random MACs: name them by owner.
    owners = link_local_owners(
        {n: sections(v["raw"]) for n, v in dump["nodes"].items() if "raw" in v}
    )
    for node, v in dump["nodes"].items():
        text = v.get("probes")
        if not text or text.startswith("error") or "raw" not in v:
            continue
        order = {}
        for r in parse_json(sections(v["raw"]).get("route4", "")) or []:
            hops = r.get("nexthops") or []
            if r.get("table", "main") == "main" and len(hops) > 1:
                order[r.get("dst")] = [
                    f"{owners.get(gateway(h), gateway(h))}@{h.get('dev')}" for h in hops
                ]
        for line in text.splitlines():
            parts = line.split("|", 4)
            if len(parts) != 5 or parts[0] != "P":
                continue
            _, prefix, src, dst, answer = parts
            hop = _hop(answer, owners)
            members = order.get(prefix, [])
            pos = members.index(hop) if hop in members else -1
            out[(node, prefix, src, dst)] = (hop, pos)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    summary = {}
    for mode in ("native", "firecracker"):
        groups: dict[str, dict[str, dict]] = defaultdict(dict)
        for run_dir in sorted((args.root / mode).glob("run_*")):
            meta_path, dump = run_dir / "meta.json", last_dump(run_dir)
            if not meta_path.exists() or dump is None:
                continue
            meta = json.loads(meta_path.read_text())
            p = probes(json.loads(dump.read_text()))
            if p:
                # Firecracker copies share the lab image's guest kernel boot.
                boot = (
                    "lab-image" if mode == "firecracker" else meta.get("boot_id", "?")
                )
                groups[boot][run_dir.name] = p
        for boot, runs in groups.items():
            if len(runs) < 2:
                continue
            names = sorted(runs)
            ref = runs[names[0]]
            rows = []
            for name in names[1:]:
                other = runs[name]
                common = ref.keys() & other.keys()
                changed = sum(1 for k in common if ref[k][0] != other[k][0])
                # Position agreement covers flows that took an ECMP member in both.
                members = [k for k in common if ref[k][1] >= 0 and other[k][1] >= 0]
                same_pos = sum(1 for k in members if ref[k][1] == other[k][1])
                rows.append((name, len(common), changed, same_pos, len(members)))
            total = sum(r[1] for r in rows)
            changed = sum(r[2] for r in rows)
            same_pos = sum(r[3] for r in rows)
            ecmp_flows = sum(r[4] for r in rows)
            runs_changed = sum(1 for r in rows if r[2])
            summary.setdefault(mode, []).append(
                {
                    "boot": boot,
                    "runs": len(names),
                    "reference": names[0],
                    "flows_per_run": len(ref),
                    "runs_with_changed_flows": runs_changed,
                    "changed_flow_share": changed / total if total else 0.0,
                    "max_changed_share": max(
                        (r[2] / r[1] for r in rows if r[1]), default=0.0
                    ),
                    "position_agreement": same_pos / ecmp_flows if ecmp_flows else 1.0,
                    "per_run": rows,
                }
            )
            print(
                f"=== {args.root.name} [{mode}] boot {boot[:8]}: {len(names)} runs, {len(ref)} probed flows/run;"
                f" {runs_changed}/{len(rows)} runs differ from {names[0]};"
                f" {100 * changed / max(total, 1):.1f}% of flows change next hop"
                f" (max {100 * summary[mode][-1]['max_changed_share']:.1f}%);"
                f" position agreement {100 * same_pos / max(ecmp_flows, 1):.1f}%"
            )
    (args.root / "flows.json").write_text(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
