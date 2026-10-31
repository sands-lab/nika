"""Decide data-plane equivalence of converged runs with KATch (NetKAT).

Each run's exported Linux FIBs become one NetKAT forwarding relation over
locations (nodes, per-interface delivery points and an ``exit`` sink) and the
32 destination-address bits:

* a node delivers packets for its own interface addresses (Linux's local table
  is consulted before main);
* main-table routes apply by longest prefix match (lower metric wins for the
  same prefix), each one restricted to the destinations no more specific route
  covers;
* a gateway hop moves the packet to the node owning the gateway address (IPv4,
  or the IPv6 link-local next hop of RFC 5549 routes); an on-link route moves
  it to the node owning the destination address on that subnet; unknown
  next hops lead to ``exit``; blackhole routes and missing routes drop;
* ECMP follows ``--ecmp``: ``set`` makes the next hops a NetKAT union, so
  their order is irrelevant; ``hash`` models Linux hash-threshold selection,
  where a 31-bit flow hash ``h`` picks the next hop by its position and weight.

Runs are clustered by KATch's equivalence decision on the whole relation
(``wf ⋅ netA ≡ wf ⋅ netB``; ``wf`` limits every address bit to 0 or 1, since
NetKAT fields are unbounded). For a run that differs from the reference run,
every destination is also checked hop by hop (``wf ⋅ d ⋅ netA ≡ wf ⋅ d ⋅
netB``) and end to end (``wf ⋅ d ⋅ netA⋆ ⋅ final ≡ wf ⋅ d ⋅ netB⋆ ⋅ final``,
where ``final`` is a delivery point or ``exit``), giving the number of
destinations whose forwarding differs.

SR Linux nodes are read from their route table (``srl_fib``), with set
semantics for ECMP, since their datapath does not use Linux's hash.

Host management networks (Docker's default bridge used by Kathara bridged
interfaces, Containerlab's per-lab management subnets) are excluded.

Not modeled: NAT (kube-proxy, Docker), policy routing beyond the main table,
TTL and ARP. Destinations are interface addresses plus host routes nobody
owns (service VIPs).

    uv run python -m experiment.convergence.netkat_equiv runtime/convergence/k8s_lab_na
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
from collections import defaultdict
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Any

import docker

from experiment.convergence.parse import (
    failed_nodes,
    gateway,
    is_mgmt,
    load_runs,
    parse_json,
    sections,
)
from nika.net_env.isp.fib import srl_fib
from nika.validation.netkat.model import prefix_test, union
from nika.validation.netkat.verifier import KATCH_IMAGE, KATCH_JAR

ADAPTER = Path(__file__).resolve().parents[2] / "src/nika/validation/netkat"
DROP_TYPES = ("blackhole", "unreachable", "prohibit")


class OnLink(list):
    """Next hops of a route whose prefix is (also) attached to the node.

    The list holds the route's gateway next hops, if any; the attached part is
    expanded into the subnet's known hosts.
    """


def srl_fibs(nodes: dict[str, dict[str, str]]) -> dict[str, tuple]:
    """Parsed SR Linux route tables; raises ValueError for an unusable one."""
    return {
        node: srl_fib(parse_json(secs["srlfib"]))
        for node, secs in nodes.items()
        if parse_json(secs.get("srlfib", ""))
    }


def unusable_nodes(doc: dict) -> set[str]:
    """Nodes whose dump failed or whose SR Linux FIB was mid-programming."""
    bad = failed_nodes(doc)
    for node, v in doc["nodes"].items():
        if "raw" in v and node not in bad:
            try:
                srl_fibs({node: sections(v["raw"])})
            except ValueError:
                bad.add(node)
    return bad


class Plane:
    """One run's forwarding state in node-name terms."""

    def __init__(self, dump: dict, drop: set[str], ecmp: str = "set") -> None:
        self.ecmp = ecmp
        nodes = {
            n: sections(v["raw"])
            for n, v in dump["nodes"].items()
            if "raw" in v and n not in drop
        }
        self.local: dict[str, dict[str, str]] = defaultdict(dict)  # node -> ip -> iface
        self.owner: dict[str, set[str]] = defaultdict(set)  # ip -> nodes
        self.gw_owner: dict[str, str] = {}
        # SR Linux forwards in its own datapath: read its route table instead
        # of the container's Linux (management) namespace.
        srl = srl_fibs(nodes)
        for node, fib in srl.items():
            for route in fib:
                if route.local and route.prefix.prefixlen == 32:
                    self._own(node, str(route.prefix.network_address), "srl")
        for node, secs in nodes.items():
            if node in srl:
                continue
            for link in parse_json(secs.get("addr", "")) or []:
                for a in link.get("addr_info", []):
                    ip = a.get("local", "")
                    if a.get("family") == "inet" and not ip.startswith("127."):
                        if not is_mgmt(ip):
                            self._own(node, ip, link["ifname"])
                    elif ip.startswith("fe80"):
                        self.gw_owner[ip] = node
        self.vips: set[str] = set()
        # node -> [(prefix, next hops or OnLink, hashed)]; hashed rules keep
        # Linux's nexthop order and weights for hash-threshold selection.
        kernel: dict[str, list[tuple[IPv4Network, Any, bool]]] = {
            node: self._srl_rules(node, fib) for node, fib in srl.items()
        }
        for node, secs in nodes.items():
            if node not in srl:
                kernel[node] = self._linux_rules(node, secs)
        # (prefix, next-hop locations, hashed, derived): derived rules are the
        # hosts of an on-link route, expanded where that route is their longest
        # match; a more specific kernel route keeps the rest.
        self.routes: dict[str, list[tuple[IPv4Network, list, bool, bool]]] = {}
        for node, rules in kernel.items():
            prefixes = [net for net, _, _ in rules]
            out = []
            for net, outs, hashed in rules:
                if not isinstance(outs, OnLink):
                    out.append((net, outs, hashed, False))
                    continue
                for ip, owners in self.owner.items():
                    addr = IPv4Address(ip)
                    if addr not in net or node in owners:
                        continue
                    if any(addr in p and p.prefixlen > net.prefixlen for p in prefixes):
                        continue
                    out.append((IPv4Network(f"{ip}/32"), sorted(owners), False, True))
                # Unknown hosts on the subnet leave the lab.
                out.append((net, sorted(set(outs) | {"exit"}), False, False))
            self.routes[node] = out

    def _own(self, node: str, ip: str, iface: str) -> None:
        self.local[node][ip] = iface
        self.owner[ip].add(node)
        self.gw_owner.setdefault(ip, node)

    def _srl_rules(self, node: str, fib) -> list:
        rules = []
        for route in fib:
            if route.local or is_mgmt(route.prefix):
                continue
            if route.discard or not route.hops:
                rules.append((route.prefix, [], False))
                continue
            # A "direct" next hop carries the router's own interface address:
            # the prefix is attached, not routed via a gateway.
            gateways = [
                h.gateway
                for h in route.hops
                if h.gateway and h.gateway not in self.local[node]
            ]
            outs = sorted({self.gw_owner.get(g, "exit") for g in gateways})
            if len(gateways) < len(route.hops):
                outs = OnLink(outs)
            rules.append((route.prefix, outs, False))
        return rules

    def _linux_rules(self, node: str, secs: dict[str, str]) -> list:
        best: dict[IPv4Network, dict] = {}
        for r in parse_json(secs.get("route4", "")) or []:
            if r.get("table", "main") != "main":
                continue
            if r.get("type") not in (None, "unicast") + DROP_TYPES:
                continue
            dst = r.get("dst")
            try:
                net = IPv4Network(
                    "0.0.0.0/0" if dst == "default" else dst, strict=False
                )
            except ValueError:
                continue  # IPv6 entries some kernels list in table all
            if is_mgmt(net):
                continue
            if net not in best or r.get("metric", 0) < best[net].get("metric", 0):
                best[net] = r
            if net.prefixlen == 32 and str(net.network_address) not in self.owner:
                self.vips.add(str(net.network_address))
        rules = []
        for net, r in best.items():
            if r.get("type") in DROP_TYPES:
                rules.append((net, [], False))
                continue
            hops = r.get("nexthops") or [r]
            if any(gateway(h) is None for h in hops):
                gws = {gateway(h) for h in hops} - {None}
                rules.append(
                    (
                        net,
                        OnLink(sorted({self.gw_owner.get(g, "exit") for g in gws})),
                        False,
                    )
                )
            elif self.ecmp == "hash" and len(hops) > 1:
                ordered = [
                    (self.gw_owner.get(gateway(h), "exit"), int(h.get("weight", 1)))
                    for h in hops
                ]
                rules.append((net, ordered, True))
            else:
                outs = sorted({self.gw_owner.get(gateway(h), "exit") for h in hops})
                rules.append((net, outs, False))
        return rules

    def destinations(self) -> set[str]:
        return set(self.owner) | self.vips

    def locations(self) -> set[str]:
        names = set(self.routes) | {"exit"}
        for node, addrs in self.local.items():
            names |= {f"dlv:{node}:{iface}" for iface in addrs.values()}
        return names

    def relation(self, ids: dict[str, int]) -> str:
        terms = []
        for node, rules in sorted(self.routes.items()):
            sw = ids[node]
            # Local addresses first: they win over every main-table route.
            taken = [IPv4Network(f"{ip}/32") for ip in self.local.get(node, {})]
            for ip, iface in sorted(self.local.get(node, {}).items()):
                terms.append(
                    f"@sw={sw} ⋅ ({prefix_test(ip + '/32')}) ⋅ @sw←{ids[f'dlv:{node}:{iface}']}"
                )
            # Longest prefix first. A route the kernel holds for a prefix wins
            # over a host derived from an on-link route of the same length.
            ordered = sorted(rules, key=lambda rule: (-rule[0].prefixlen, rule[3]))
            for net, outs, hashed, _derived in ordered:
                surviving = [net]
                for higher in taken:
                    remaining = []
                    for p in surviving:
                        if not p.overlaps(higher):
                            remaining.append(p)
                        elif not p.subnet_of(higher):
                            remaining.extend(p.address_exclude(higher))
                    surviving = remaining
                taken.append(net)
                if not surviving or not outs:
                    continue  # shadowed, or dropped (⊥)
                guard = union(prefix_test(str(p)) for p in surviving)
                if hashed:
                    action = union(
                        f"({hash_test(lo, hi)}) ⋅ @sw←{ids[o]}"
                        for (o, _), (lo, hi) in zip(
                            outs, hash_buckets([w for _, w in outs])
                        )
                    )
                else:
                    action = union(f"@sw←{ids[o]}" for o in outs)
                terms.append(f"@sw={sw} ⋅ ({guard}) ⋅ ({action})")
        return union(terms)


HASH_BITS = 31  # Linux compares a 31-bit flow hash with nexthop upper bounds


def hash_buckets(weights: list[int]) -> list[tuple[int, int]]:
    """Hash ranges of Linux hash-threshold ECMP (``fib_rebalance``, ``nh_group_rebalance``).

    Nexthop i gets upper bound ``round((w_1 + .. + w_i) << 31 / total) - 1``; a
    flow takes the first nexthop whose upper bound is at least its hash.
    """
    total, cumulative, low, out = sum(weights), 0, 0, []
    for w in weights:
        cumulative += w
        upper = ((cumulative << HASH_BITS) + total // 2) // total - 1
        out.append((low, upper))
        low = upper + 1
    return out


def hash_test(low: int, high: int) -> str:
    """NetKAT test for ``low <= h <= high`` over hash bits ``@h0`` (MSB) .. ``@h30``."""
    terms, start = [], low
    while start <= high:
        size = start & -start if start else 1 << HASH_BITS
        while size > high - start + 1:
            size >>= 1
        length = HASH_BITS - size.bit_length() + 1
        bits = f"{start >> (HASH_BITS - length):0{length}b}" if length else ""
        terms.append(" ⋅ ".join(f"@h{i}={b}" for i, b in enumerate(bits)) or "⊤")
        start += size
    return union(terms)


def run_katch(defs: dict[str, str], queries: list[str], workdir: Path) -> list[str]:
    """Return 'passed' / 'failed' per query, in order."""
    (workdir / "network.nkpl").write_text(
        "".join(f"{k} = {v}\n" for k, v in defs.items())
    )
    qdir = workdir / "queries"
    qdir.mkdir(exist_ok=True)
    for i, q in enumerate(queries):
        (qdir / f"query-{i:06d}.nkpl").write_text(q + "\n")
    # The Docker daemon may not read the checkout (root-squashed NFS homes).
    adapter = workdir / "adapter"
    adapter.mkdir(exist_ok=True)
    (adapter / "KatchBatch.java").write_text((ADAPTER / "KatchBatch.java").read_text())
    client = docker.from_env()
    container = client.containers.create(
        KATCH_IMAGE,
        entrypoint=["/bin/sh", "-c"],
        command=[
            f"javac -J-Xmx256m -cp {KATCH_JAR} -d /tmp /adapter/KatchBatch.java"
            f" && exec java -Xmx6g -Xss64m -cp /tmp:{KATCH_JAR}"
            " KatchBatch /work/network.nkpl /work/queries"
        ],
        network_disabled=True,
        mem_limit="8g",
        volumes={
            str(workdir.resolve()): {"bind": "/work", "mode": "ro"},
            str(adapter.resolve()): {"bind": "/adapter", "mode": "ro"},
        },
    )
    try:
        container.start()
        status = container.wait(timeout=3600)["StatusCode"]
        log = container.logs().decode(errors="replace")
    finally:
        container.remove(force=True)
        client.close()
    (workdir / "batch.log").write_text(log)
    clean = re.sub(r"\x1b\[[0-9;]*m", "", log)
    results: dict[int, str] = {}
    for result, number in re.findall(
        r"Check (passed|failed) in /work/queries/query-(\d+)\.nkpl", clean
    ):
        results.setdefault(int(number), result)
    if status != 0 or len(results) != len(queries):
        raise RuntimeError(
            f"KATch failed (status {status}); see {workdir / 'batch.log'}"
        )
    return [results[i] for i in range(len(queries))]


def compare_pair(a: Plane, b: Plane, workdir: Path, detail: bool) -> dict[str, Any]:
    names = sorted(a.locations() | b.locations())
    ids = {n: i for i, n in enumerate(names, 1)}
    finals = union(
        f"@sw={ids[n]}" for n in names if n.startswith("dlv:") or n == "exit"
    )
    # NetKAT fields are unbounded, so restrict inputs to packets whose 32
    # address bits are 0 or 1; otherwise equal forwarding written with
    # different prefix splits would differ on impossible headers.
    wf = " ⋅ ".join(f"(@d{i}=0 + @d{i}=1)" for i in range(32))
    if "hash" in (a.ecmp, b.ecmp):
        wf += " ⋅ " + " ⋅ ".join(f"(@h{i}=0 + @h{i}=1)" for i in range(HASH_BITS))
    defs = {"wf": wf, "netA": a.relation(ids), "netB": b.relation(ids), "fin": finals}
    queries = ["check wf ⋅ netA ≡ wf ⋅ netB"]
    dests = sorted(a.destinations() | b.destinations(), key=lambda ip: IPv4Address(ip))
    if detail:
        for d in dests:
            t = prefix_test(d + "/32")
            queries.append(f"check wf ⋅ ({t}) ⋅ netA ≡ wf ⋅ ({t}) ⋅ netB")
            queries.append(
                f"check wf ⋅ ({t}) ⋅ (netA)⋆ ⋅ fin ≡ wf ⋅ ({t}) ⋅ (netB)⋆ ⋅ fin"
            )
    out = run_katch(defs, queries, workdir)
    report: dict[str, Any] = {
        "equivalent": out[0] == "passed",
        "destinations": len(dests),
    }
    if detail:
        hop = [d for d, r in zip(dests, out[1::2]) if r == "failed"]
        e2e = [d for d, r in zip(dests, out[2::2]) if r == "failed"]
        report.update(hop_differs=hop, e2e_differs=e2e)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument(
        "--mode", default=None, help="native or firecracker (default: both)"
    )
    parser.add_argument(
        "--ecmp",
        choices=["set", "hash"],
        default="set",
        help="ECMP semantics: any next hop (set) or Linux hash-threshold by nexthop order (hash)",
    )
    args = parser.parse_args()
    modes = (
        [args.mode]
        if args.mode
        else [m for m in ("native", "firecracker") if (args.root / m).is_dir()]
    )
    summary = {}
    for mode in modes:
        runs = load_runs(args.root, mode)
        if len(runs) < 2:
            continue
        drop = set().union(*(unusable_nodes(d) for d in runs.values()))
        planes = {r: Plane(d, drop, args.ecmp) for r, d in runs.items()}
        classes: list[list[str]] = []
        diffs: dict[str, Any] = {}
        with tempfile.TemporaryDirectory(prefix="nika-katch-") as tmp:
            for run, plane in planes.items():
                for cls in classes:
                    work = Path(tmp) / f"{run}-vs-{cls[0]}"
                    work.mkdir()
                    if compare_pair(planes[cls[0]], plane, work, detail=False)[
                        "equivalent"
                    ]:
                        cls.append(run)
                        break
                else:
                    classes.append([run])
            ref = classes[0][0]
            for cls in classes[1:]:
                work = Path(tmp) / f"detail-{cls[0]}"
                work.mkdir()
                diffs[cls[0]] = compare_pair(
                    planes[ref], planes[cls[0]], work, detail=True
                )
        summary[mode] = {
            "runs": len(runs),
            "dropped_nodes": sorted(drop),
            "classes": classes,
            "diffs": diffs,
        }
        print(
            f"=== {args.root.name} [{mode}] {len(runs)} runs: {len(classes)} data-plane equivalence class(es)"
        )
        print(
            f"    classes: {[len(c) for c in classes]}"
            + (f"  dropped nodes: {sorted(drop)}" if drop else "")
        )
        for rep, d in diffs.items():
            print(
                f"    {rep} vs {ref}: hop-level differs for {len(d['hop_differs'])}/{d['destinations']} destinations,"
                f" end-to-end for {len(d['e2e_differs'])}: {d['e2e_differs'][:6]}"
            )
    # Keep results of modes not analyzed in this invocation.
    out = args.root / f"netkat_{args.ecmp}.json"
    previous = json.loads(out.read_text()) if out.exists() else {}
    out.write_text(json.dumps({**previous, **summary}, indent=1))


if __name__ == "__main__":
    main()
