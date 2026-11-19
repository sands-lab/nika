"""Joint integer programming for network-disjoint release datasets."""

from __future__ import annotations

import json
import warnings
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

from nika.firecracker.image import variant_for_row, vm_memory_mib
from nika.problems.combinations import combination_for_row
from nika.workflows.benchmark.admit import HEAVY_CLASSES, resource_class_for_row
from nika.workflows.benchmark.candidate_context import row_to_case
from nika.workflows.benchmark.legacy_retention import match as legacy_match
from nika.workflows.benchmark.load_config import (
    load_candidate_catalog,
    normalize_benchmark_row,
)
from nika.workflows.benchmark.multi_fault import row_problems
from nika.workflows.benchmark.network_identity import (
    MAIN_SLICES,
    SIZE_EXCEPTIONS,
    VENDORS,
    VENDOR_TWIN_LIMIT,
    TIER_A_GRAPHS,
    dataset_row,
    is_tier_a_cell,
    size_signature,
    twin_signature,
)
from nika.workflows.benchmark.release import (
    REQUIRED_SPLIT_BACKENDS,
    REQUIRED_SPLIT_FAMILIES,
    REQUIRED_SPLIT_SCALES,
    ReleaseError,
    _benchmark_failures,
    _validate_release_splits,
    _validate_release_combinations,
)
from nika.workflows.benchmark.trials import task_id_for_row

Row = dict[str, Any]
SCALES = ("s", "m", "l")
FACTORS = ("failure_domain", "family", "topo_scale", "backend", "router_platform")
HEALTHY_COUNT = 19
THIRD_SIZE_COUNT = 24
CORE_SIZE_PAIR_MIN_SHARE = 0.25
MAX_VM_MEMORY_GIB = 38


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _heavy(row: Row) -> int:
    return int(resource_class_for_row(row) in HEAVY_CLASSES)


def factor_weights(row: Row, factor: str) -> dict[str, float]:
    """Give each task unit marginal mass, shared across its failure domains."""
    values = row[factor].split("+") if factor == "failure_domain" else [row[factor]]
    return {v: 1 / len(values) for v in values}


def _distribution(rows, factor):
    counts = Counter()
    for row in rows:
        counts.update(factor_weights(row, factor))
    return dict(sorted(counts.items()))


def total_variation(dev: list[Row], test: list[Row], factor: str) -> float:
    """Compare the equal-weight main-task distributions."""
    dev = [r for r in dev if r["slice"] in MAIN_SLICES]
    test = [r for r in test if r["slice"] in MAIN_SLICES]
    a, b = _distribution(dev, factor), _distribution(test, factor)
    if not dev or not test:
        return 1.0
    return (
        sum(
            abs(a.get(v, 0) / len(dev) - b.get(v, 0) / len(test))
            for v in sorted(a.keys() | b.keys())
        )
        / 2
    )


class _Program:
    """Sparse binary program with continuous absolute-deviation variables."""

    def __init__(self, seed: int):
        self.seed = seed
        self.labels: list[Any] = []
        self.integral: list[int] = []
        self.bounds: list[float] = []
        self.constraints: list[dict[int, float]] = []
        self.lower: list[float] = []
        self.upper: list[float] = []
        self.stages: list[Row] = []

    def var(self, label: Any, *, continuous: bool = False) -> int:
        self.labels.append(label)
        self.integral.append(0 if continuous else 1)
        self.bounds.append(np.inf if continuous else 1)
        return len(self.labels) - 1

    def con(self, terms, lo=-np.inf, hi=np.inf):
        coefficients: dict[int, float] = defaultdict(float)
        for i, value in terms:
            coefficients[i] += value
        self.constraints.append(dict(coefficients))
        self.lower.append(lo)
        self.upper.append(hi)

    def absolute(self, label, terms, constant=0):
        z = self.var(label, continuous=True)
        terms = list(terms)
        self.con(terms + [(z, -1)], hi=-constant)
        self.con([(i, -v) for i, v in terms] + [(z, -1)], hi=constant)
        return z

    def solve(self, objective: dict[int, float], name: str):
        rr, cc, values = [], [], []
        for j, terms in enumerate(self.constraints):
            for i, value in sorted(terms.items()):
                rr.append(j)
                cc.append(i)
                values.append(value)
        matrix = coo_matrix(
            (values, (rr, cc)), shape=(len(self.constraints), len(self.labels))
        ).tocsc()
        cost = np.zeros(len(self.labels))
        for i, value in objective.items():
            cost[i] = value
        # HiGHS receives these options through SciPy. A single thread, ordered
        # inputs and no wall-clock cutoff make each stage reproducible.
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Unrecognized options detected")
            result = milp(
                cost,
                integrality=np.array(self.integral),
                bounds=Bounds(0, self.bounds),
                constraints=LinearConstraint(matrix, self.lower, self.upper),
                options={
                    "mip_rel_gap": 0.0,
                    "threads": 1,
                    "random_seed": self.seed & 0x7FFFFFFF,
                },
            )
        if result.status == 2:
            raise ReleaseError(f"Joint split constraints are infeasible ({name})")
        if result.status != 0:
            raise ReleaseError(
                f"Joint split solve did not prove optimality ({name}): {result.message}"
            )
        self.stages.append(
            {
                "stage": name,
                "objective": float(result.fun),
                "dual_bound": float(result.mip_dual_bound),
                "gap": float(result.mip_gap),
                "nodes": int(result.mip_node_count),
                "optimal": True,
            }
        )
        return result.x


def _solve(
    rows: list[Row], *, seed: int, multi_dev: int, legacy_retention: bool = True
):
    p = _Program(seed)
    vendor_lookup: dict[Any, dict[str, int]] = defaultdict(dict)
    for i, r in enumerate(rows):
        if r["slice"] == "core" and r["device_profile"] in VENDORS:
            vendor_lookup[twin_signature(r)].setdefault(r["device_profile"], i)
    twins = {
        i: vendor_lookup.get(twin_signature(r), {})
        for i, r in enumerate(rows)
        if r["slice"] == "core" and r["device_profile"] == "frr"
    }
    default_sizes = defaultdict(set)
    for r in rows:
        if r["slice"] == "core" and r["device_profile"] not in VENDORS:
            default_sizes[r["problem"], size_signature(r)].add(r["topo_scale"])
    default_faults = {
        fault for (fault, _), sizes in default_sizes.items() if len(sizes) >= 2
    }
    scores = {
        (s, i): legacy_match(r, s)[0]
        for i, r in enumerate(rows)
        for s in ("dev", "test")
    }
    representatives = {}
    for i, r in enumerate(rows):
        if r["slice"] != "core":
            continue
        # Prefer the default platform when its pool supports a matched pair.
        # Native-only inventories retain their supported platforms.
        if r["device_profile"] in VENDORS and r["problem"] in default_faults:
            continue
        key = (
            r["problem"],
            r["network_id"],
            size_signature(r),
            tuple(sorted(twins.get(i, {}))),
            (
                scores["dev", i],
                scores["test", i],
                tuple(
                    (v, scores["dev", j], scores["test", j])
                    for v, j in sorted(twins.get(i, {}).items())
                ),
            )
            if legacy_retention
            else (),
        )
        representatives.setdefault(key, i)
    # At a fixed network and size contract, a representative with fewer
    # available vendors is dominated. Injection targets are free across sizes.
    # Retain incomparable vendor sets, so this reduction preserves capacity.
    cells = defaultdict(list)
    for key, i in representatives.items():
        cells[key[:3], key[4]].append((frozenset(key[3]), i))
    ids = [
        i
        for cell in cells.values()
        for vendors, i in cell
        if not any(vendors < other for other, _ in cell)
    ]
    by_fault: dict[str, list[int]] = defaultdict(list)
    for i in ids:
        by_fault[rows[i]["problem"]].append(i)
    networks = sorted({r["network_id"] for r in rows})
    y = {n: p.var(("network_dev", n)) for n in networks}
    matched_networks = sorted({f"isp_graph:{g}" for g in TIER_A_GRAPHS} & y.keys())
    for n in matched_networks[1:]:
        p.con([(y[n], 1), (y[matched_networks[0]], -1)], 0, 0)
    x = {(s, i): p.var(("core", s, i)) for s in ("dev", "test") for i in ids}
    family_networks = defaultdict(set)
    for r in rows:
        family_networks[r["family"]].add(r["network_id"])
    for family in sorted(REQUIRED_SPLIT_FAMILIES):
        ns = family_networks[family]
        # A required third size on a single generated s/m/l inventory uses two
        # Dev networks and leaves the remaining network for the Test core.
        if (
            len(ns) == 3
            and all(not n.startswith("isp_graph:") for n in ns)
            and len({n.split(":")[0] for n in ns}) == 1
        ):
            p.con(((y[n], 1) for n in sorted(ns)), 2, 2)
    for fault, options in sorted(by_fault.items()):
        for s in ("dev", "test"):
            p.con(((x[s, i], 1) for i in options), 1, 1)
        signatures, scales = defaultdict(list), defaultdict(list)
        for i in options:
            signatures[size_signature(rows[i])].append(i)
            scales[rows[i]["topo_scale"]].append(i)
        for group in signatures.values():
            p.con(
                [(x["dev", i], 1) for i in group] + [(x["test", i], -1) for i in group],
                0,
                0,
            )
        if fault not in SIZE_EXCEPTIONS:
            for group in scales.values():
                p.con(((x[s, i], 1) for s in ("dev", "test") for i in group), hi=1)
        else:
            for i in options:
                if rows[i]["family"] != "isp" or rows[i]["topo_scale"] != "s":
                    for s in ("dev", "test"):
                        p.con([(x[s, i], 1)], 0, 0)

    family_sizes = defaultdict(set)
    for i in ids:
        r = rows[i]
        family_sizes[r["problem"], r["family"]].add(r["topo_scale"])
    size_balanced_ids = [
        i
        for i in ids
        if rows[i]["problem"] not in SIZE_EXCEPTIONS
        and set(SCALES) <= family_sizes[rows[i]["problem"], rows[i]["family"]]
    ]
    for omitted in SCALES:
        # Each eligible pair uses two different sizes. Its family indicator
        # minus its cores at the omitted size counts this exact pair pattern.
        p.con(
            [(x["dev", i], 1 - CORE_SIZE_PAIR_MIN_SHARE) for i in size_balanced_ids]
            + [
                (x[s, i], -1)
                for s in ("dev", "test")
                for i in size_balanced_ids
                if rows[i]["topo_scale"] == omitted
            ],
            lo=0,
        )

    def ownership(z, s, i):
        p.con(
            [(z, 1), (y[rows[i]["network_id"]], -1 if s == "dev" else 1)],
            hi=0 if s == "dev" else 1,
        )

    for (s, i), z in x.items():
        ownership(z, s, i)
    for s in ("dev", "test"):
        for factor, values in (
            ("family", REQUIRED_SPLIT_FAMILIES),
            ("topo_scale", REQUIRED_SPLIT_SCALES),
        ):
            for value in sorted(values):
                p.con(((x[s, i], 1) for i in ids if rows[i][factor] == value), lo=1)
    multis = [i for i, r in enumerate(rows) if r["slice"] == "multi_fault"]
    mc = Counter(rows[i]["network_id"] for i in multis)
    p.con(((y[n], c) for n, c in sorted(mc.items())), multi_dev, multi_dev)

    health_ids = [i for i, r in enumerate(rows) if r["slice"] == "healthy"]
    h = {(s, i): p.var(("healthy", s, i)) for s in ("dev", "test") for i in health_ids}
    for (s, i), z in h.items():
        ownership(z, s, i)
    for s in ("dev", "test"):
        p.con(((h[s, i], 1) for i in health_ids), HEALTHY_COUNT, HEALTHY_COUNT)

    for s in ("dev", "test"):
        for backend in sorted(REQUIRED_SPLIT_BACKENDS):
            terms = [
                (z, 1)
                for (ss, i), z in (x | h).items()
                if ss == s and rows[i]["backend"] == backend
            ]
            matches = Counter(
                rows[i]["network_id"] for i in multis if rows[i]["backend"] == backend
            )
            terms += [
                (y[n], c if s == "dev" else -c) for n, c in sorted(matches.items())
            ]
            p.con(terms, lo=1 if s == "dev" else 1 - sum(matches.values()))

    pairs = {}
    for fault, options in sorted(by_fault.items()):
        for vendor in VENDORS:
            group = []
            for i in options:
                if vendor not in twins.get(i, {}):
                    continue
                for s in ("dev", "test"):
                    z = p.var(("vendor_twin", s, i, vendor))
                    pairs[s, i, vendor] = z
                    group.append((z, 1))
                    p.con([(z, 1), (x[s, i], -1)], hi=0)
            # Each vendor/fault comparison contributes one matched pair.
            p.con(group, hi=1)
    tier_a = {z for (s, i, vendor), z in pairs.items() if is_tier_a_cell(rows[i])}
    # A matched cell contributes all four vendors sharing one FRR core.
    for (s, i), core in x.items():
        if not is_tier_a_cell(rows[i]):
            continue
        if set(twins.get(i, {})) != set(VENDORS):
            p.con([(core, 1)], 0, 0)
            continue
        for vendor in VENDORS:
            p.con([(pairs[s, i, vendor], 1), (core, -1)], 0, 0)
    p.con(((z, 1) for z in pairs.values()), hi=VENDOR_TWIN_LIMIT)

    thirds = {}
    third_cells = set()
    for i in sorted(ids, key=lambda i: -scores["dev", i] if legacy_retention else 0):
        r = rows[i]
        cell = (r["problem"], r["network_id"], size_signature(r))
        if r["problem"] in SIZE_EXCEPTIONS or cell in third_cells:
            continue
        third_cells.add(cell)
        z = p.var(("third_size", i))
        thirds[i] = z
        ownership(z, "dev", i)
        compatible = [
            j
            for j in by_fault[r["problem"]]
            if size_signature(rows[j]) == size_signature(r)
        ]
        for s in ("dev", "test"):
            p.con([(z, 1)] + [(x[s, j], -1) for j in compatible], hi=0)
            p.con(
                [(z, 1)]
                + [
                    (x[s, j], 1)
                    for j in by_fault[r["problem"]]
                    if rows[j]["topo_scale"] == r["topo_scale"]
                ],
                hi=1,
            )
    for fault, options in sorted(by_fault.items()):
        p.con(((thirds[i], 1) for i in options if i in thirds), hi=1)
    p.con(((z, 1) for z in thirds.values()), THIRD_SIZE_COUNT, THIRD_SIZE_COUNT)
    for family in sorted(REQUIRED_SPLIT_FAMILIES):
        p.con(((z, 1) for i, z in thirds.items() if rows[i]["family"] == family), 1, 6)

    # With a fixed multi-fault quota the two main denominators are constants.
    # Cross multiplication encodes exact TVDs, including every catalog task.
    nd = len(by_fault) + HEALTHY_COUNT + multi_dev
    nt = len(by_fault) + HEALTHY_COUNT + len(multis) - multi_dev
    excess = []
    for factor in FACTORS:
        deviations = []
        for value in sorted({v for r in rows for v in factor_weights(r, factor)}):
            terms = [
                (
                    z,
                    (nt if s == "dev" else -nd)
                    * factor_weights(rows[i], factor).get(value, 0),
                )
                for (s, i), z in h.items()
                if value in factor_weights(rows[i], factor)
            ]
            # Matched cores have identical non-size factors. Cancel their two
            # sums explicitly to strengthen the relaxation without changing it.
            if factor == "topo_scale":
                terms += [
                    (z, nt if s == "dev" else -nd)
                    for (s, i), z in x.items()
                    if value in factor_weights(rows[i], factor)
                ]
            elif nd != nt:
                terms += [
                    (z, nt - nd)
                    for (s, i), z in x.items()
                    if s == "dev" and rows[i][factor] == value
                ]
            matches = Counter()
            for i in multis:
                matches[rows[i]["network_id"]] += factor_weights(rows[i], factor).get(
                    value, 0
                )
            terms += [(y[n], c * (nd + nt)) for n, c in sorted(matches.items())]
            deviations.append(
                p.absolute(("tvd", factor, value), terms, -nd * sum(matches.values()))
            )
        z = p.var(("tvd_excess", factor), continuous=True)
        p.con([(d, 1) for d in deviations] + [(z, -1)], hi=0.1 * nd * nt)
        excess.append(z)
    solution = p.solve(dict.fromkeys(sorted(tier_a), -1), "tier_a_capacity")
    tier_a_capacity = round(sum(solution[z] for z in tier_a))
    p.con(((z, 1) for z in sorted(tier_a)), tier_a_capacity, tier_a_capacity)
    solution = p.solve(dict.fromkeys(excess, 1), "main_tvd_excess")
    p.con(((z, 1) for z in excess), hi=sum(solution[z] for z in excess) + 1e-7)
    solution = p.solve(dict.fromkeys(pairs.values(), -1), "vendor_capacity")
    capacity = round(sum(solution[z] for z in pairs.values()))
    p.con(((z, 1) for z in pairs.values()), capacity, capacity)
    domains = sorted({rows[i]["failure_domain"] for i in thirds})
    domain_deviation = [
        p.absolute(
            ("third_domain", d),
            [
                (z, len(domains))
                for i, z in thirds.items()
                if rows[i]["failure_domain"] == d
            ],
            -THIRD_SIZE_COUNT,
        )
        for d in domains
    ]
    solution = p.solve(dict.fromkeys(domain_deviation, 1), "third_size_domains")
    optimum = round(sum(solution[z] for z in domain_deviation))
    p.con(((z, 1) for z in domain_deviation), optimum, optimum)
    if legacy_retention:
        legacy_cost = {z: -scores[s, i] for (s, i), z in (x | h).items()}
        legacy_cost.update(
            {z: -scores[s, twins[i][v]] for (s, i, v), z in pairs.items()}
        )
        legacy_cost.update({z: -scores["dev", i] for i, z in thirds.items()})
        for i in multis:
            z = y[rows[i]["network_id"]]
            legacy_cost[z] = (
                legacy_cost.get(z, 0) - scores["dev", i] + scores["test", i]
            )
        solution = p.solve(legacy_cost, "legacy_retention")
        optimum = round(sum(value * solution[z] for z, value in legacy_cost.items()))
        p.con(legacy_cost.items(), optimum, optimum)
    costs = {z: _heavy(rows[i]) for (s, i), z in (x | h).items()}
    costs.update({z: _heavy(rows[twins[i][v]]) for (s, i, v), z in pairs.items()})
    costs.update({z: _heavy(rows[i]) for i, z in thirds.items()})
    solution = p.solve(costs, "heavy_cost")
    datasets: dict[str, list[Row]] = {"dev": [], "test": []}
    for (s, i), z in (x | h).items():
        if solution[z] > 0.5:
            row = dict(rows[i])
            if row["problem"] in SIZE_EXCEPTIONS:
                row["size_comparison_excluded"] = True
            datasets[s].append(row)
    cores = {
        fault: [
            r
            for rs in datasets.values()
            for r in rs
            if r["slice"] == "core" and r["problem"] == fault
        ]
        for fault in by_fault
    }
    for (s, i, vendor), z in pairs.items():
        if solution[z] > 0.5:
            datasets[s].append(
                {
                    **rows[twins[i][vendor]],
                    "slice": "vendor_twin",
                    "vendor_tier": "A" if is_tier_a_cell(rows[i]) else "C",
                    "base_task_id": task_id_for_row(normalize_benchmark_row(rows[i])),
                }
            )
    for i, z in thirds.items():
        if solution[z] > 0.5:
            datasets["dev"].append(
                {
                    **rows[i],
                    "slice": "third_size",
                    "core_task_ids": sorted(
                        task_id_for_row(normalize_benchmark_row(r))
                        for r in cores[rows[i]["problem"]]
                    ),
                }
            )
    assignment = {n: "dev" if solution[z] > 0.5 else "test" for n, z in y.items()}
    for i in multis:
        datasets[assignment[rows[i]["network_id"]]].append(dict(rows[i]))
    datasets["dev_pool"] = [r for r in rows if assignment[r["network_id"]] == "dev"]
    capacity_by_vendor = {
        v: len({rows[i]["problem"] for i in ids if v in twins.get(i, {})})
        for v in VENDORS
    }
    return datasets, {
        "network_assignment": assignment,
        "core_size_pair_balance": {
            "minimum_share": CORE_SIZE_PAIR_MIN_SHARE,
            "eligible_faults": int(
                sum(solution[x["dev", i]] > 0.5 for i in size_balanced_ids)
            ),
        },
        "solver": {
            "implementation": "scipy.optimize.milp / HiGHS",
            "stages": p.stages,
            "variables": len(p.labels),
            "constraints": len(p.constraints),
            "vendor_capacity": capacity,
            "tier_a_capacity": tier_a_capacity,
            "vendor_twin_limit": VENDOR_TWIN_LIMIT,
            "single_vendor_upper_bounds": capacity_by_vendor,
        },
    }


def generate_splits(
    candidates: list[Row],
    *,
    seed: int = 42,
    max_vm_memory_gib: int = MAX_VM_MEMORY_GIB,
    legacy_retention: bool = True,
) -> tuple[dict[str, list[Row]], Row]:
    """Solve ownership, core, twins and third sizes in one deterministic program."""
    rows, excluded_timed = [], 0
    excluded_memory = []
    memory_by_variant = {}
    for original in sorted(candidates, key=_canonical):
        multi = len(row_problems(original)) > 1
        if not multi and original.get("schedule") is not None:
            excluded_timed += 1
            continue
        row = dataset_row(
            row_to_case(original),
            slice_name="multi_fault"
            if multi
            else "healthy"
            if original["problem"] == "healthy"
            else "core",
        )
        if multi:
            row["combination"] = combination_for_row(original)
        if (
            not row["network_id"].startswith("isp_graph:")
            and row["topo_scale"] != "fixed"
        ):
            row["topo_size"] = row["topo_scale"]
        variant = variant_for_row(row)
        key = _canonical(variant)
        if key not in memory_by_variant:
            memory_by_variant[key] = vm_memory_mib(variant)
        if memory_by_variant[key] > max_vm_memory_gib * 1024:
            excluded_memory.append(task_id_for_row(row))
            continue
        rows.append(row)
    inventories: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        inventories[r["network_id"].split(":")[0]].add(r["network_id"])
    excluded = {next(iter(ns)) for ns in inventories.values() if len(ns) == 1}
    rows = [r for r in rows if r["network_id"] not in excluded]
    multi_count = sum(r["slice"] == "multi_fault" for r in rows)
    # The 15/15 quota is subordinate to hard feasibility. Try the nearest
    # attainable quota when catalog placement prevents an equal split.
    attempts = []
    for multi_dev in sorted(
        range(multi_count + 1), key=lambda n: (abs(2 * n - multi_count), n)
    ):
        try:
            datasets, report = _solve(
                rows, seed=seed, multi_dev=multi_dev, legacy_retention=legacy_retention
            )
            break
        except ReleaseError as exc:
            if "infeasible" not in str(exc):
                raise
            attempts.append(str(exc) + f"; multi_fault_dev={multi_dev}")
    else:
        return {"dev": [], "test": [], "dev_pool": []}, {
            "hard_constraints": {"passed": False, "errors": attempts},
            "seed": seed,
            "parameters": {
                "max_vm_memory_gib": max_vm_memory_gib,
                "legacy_retention": legacy_retention,
            },
            "excluded_vm_memory_task_ids": sorted(excluded_memory),
            "counts": {"dev": 0, "test": 0, "dev_pool": 0},
        }
    dev, test = datasets["dev"], datasets["test"]
    errors = []
    for validate in (
        lambda: _validate_release_splits(
            dev, test, expected_failures=_benchmark_failures()
        ),
        lambda: _validate_release_combinations(dev + test),
    ):
        try:
            validate()
        except (ReleaseError, ValueError) as exc:
            errors.append(str(exc))
    for rs in datasets.values():
        rs.sort(key=lambda r: (r["slice"], task_id_for_row(r)))
    main = {
        s: [r for r in rs if r["slice"] in MAIN_SLICES]
        for s, rs in datasets.items()
        if s != "dev_pool"
    }
    cores = defaultdict(list)
    for r in dev + test:
        if r["slice"] == "core":
            cores[r["problem"]].append(r)
    report.update(
        {
            "seed": seed,
            "parameters": {
                "max_vm_memory_gib": max_vm_memory_gib,
                "legacy_retention": legacy_retention,
            },
            "excluded_vm_memory_task_ids": sorted(excluded_memory),
            "input_candidates": len(candidates),
            "deferred_single_fault_timed": excluded_timed,
            "excluded_single_networks": sorted(excluded),
            "hard_constraints": {"passed": not errors, "errors": errors},
            "quota_fallbacks": attempts,
            "counts": {s: len(rs) for s, rs in datasets.items()},
            "slice_counts": {
                s: dict(sorted(Counter(r["slice"] for r in rs).items()))
                for s, rs in datasets.items()
            },
            "vendor_pairs": {
                "tiers": {
                    tier: {
                        s: dict(
                            sorted(
                                Counter(
                                    r["device_profile"]
                                    for r in rs
                                    if r.get("vendor_tier") == tier
                                ).items()
                            )
                        )
                        for s, rs in (("dev", dev), ("test", test))
                    }
                    for tier in ("A", "C")
                },
                "counts": {
                    s: dict(
                        Counter(
                            r["device_profile"]
                            for r in rs
                            if r["slice"] == "vendor_twin"
                        )
                    )
                    for s, rs in datasets.items()
                    if s != "dev_pool"
                },
            },
            "size_patterns": dict(
                Counter(
                    "/".join(sorted((r["topo_scale"] for r in rs), key=SCALES.index))
                    for rs in cores.values()
                )
            ),
            "size_comparison_excluded": sorted(SIZE_EXCEPTIONS & cores.keys()),
            "third_size_domains": dict(
                Counter(r["failure_domain"] for r in dev if r["slice"] == "third_size")
            ),
            "third_size_families": dict(
                Counter(r["family"] for r in dev if r["slice"] == "third_size")
            ),
            "multi_fault": {
                "counts": {
                    s: sum(r["slice"] == "multi_fault" for r in rs)
                    for s, rs in main.items()
                }
            },
            "tvd_scope": "core + healthy + multi_fault; multi-domain tasks share unit mass across domains",
            "tvd": {f: total_variation(dev, test, f) for f in FACTORS},
            "distributions": {
                s: {f: _distribution(rs, f) for f in FACTORS} for s, rs in main.items()
            },
            "cost": {
                s: {
                    "main_tasks": len(main[s]),
                    "all_tasks": len(rs),
                    "main_heavy": sum(map(_heavy, main[s])),
                    "all_heavy": sum(map(_heavy, rs)),
                    "main_trials": 3 * len(main[s]),
                    "all_trials": 3 * len(rs),
                }
                for s, rs in (("dev", dev), ("test", test))
            },
        }
    )
    report["legacy_retention"] = {
        s: {
            kind: {
                "same_split": sum(legacy_match(r, s)[1:] == (kind, True) for r in rs),
                "other_split": sum(legacy_match(r, s)[1:] == (kind, False) for r in rs),
            }
            for kind in ("exact", "network_fault")
        }
        for s, rs in (("dev", dev), ("test", test))
    }
    report["tvd_misses"] = [f for f, v in report["tvd"].items() if v > 0.05 + 1e-12]
    return datasets, report


def write_split_catalog(
    *,
    pool: Path,
    output: Path,
    seed: int = 42,
    max_vm_memory_gib: int = MAX_VM_MEMORY_GIB,
    legacy_retention: bool = True,
) -> Row:
    """Write selected tasks, the Dev training pool and the joint-solve receipt."""
    datasets, report = generate_splits(
        load_candidate_catalog(pool),
        seed=seed,
        max_vm_memory_gib=max_vm_memory_gib,
        legacy_retention=legacy_retention,
    )
    output.mkdir(parents=True, exist_ok=True)
    for name, rows in datasets.items():
        (output / f"{name}.yaml").write_text(
            yaml.safe_dump(
                {
                    "cases": [
                        {
                            k: v
                            for k, v in row.items()
                            if row.get("schedule") is None
                            or k not in {"problem", "problems", "inject"}
                        }
                        for row in rows
                    ]
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
    (output / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report
