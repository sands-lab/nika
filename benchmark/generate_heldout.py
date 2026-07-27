"""Generate held-out Test split for a frozen nika-bench release.

Picks one alternate instance per Dev failure type from ``benchmark_full.yaml``
(prefer different scenario, then topo_size, then inject). Problems with no
full-matrix alternate fall back to the same scenario with inject seed 43.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections import defaultdict
from pathlib import Path

import yaml

from nika.config import BENCHMARK_DIR
from nika.workflows.benchmark.load_config import load_benchmark_yaml
from nika.workflows.benchmark.release import (
    DEFAULT_RELEASE_VERSION,
    DEFAULTS_V1,
    RESOURCES_V1,
    SCORING_V1,
    TOOLS_V1,
    build_scenario_problem_pins,
    collect_images_for_scenarios,
    releases_dir,
    verify_dev_test_isolation,
    write_release_manifest,
)
from nika.workflows.benchmark.resume import benchmark_row_fingerprint

# Offline inject resolver lives next to this script.
_BENCHMARK_DIR = Path(__file__).resolve().parent
if str(_BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(_BENCHMARK_DIR))
from inject_resolve import resolve_inject_params, validate_benchmark_case  # noqa: E402

HELDOUT_SEED = 43


def _normalize_row(row: dict) -> dict:
    topo = row.get("topo_size")
    return {
        "scenario": str(row["scenario"]),
        "topo_size": None if topo in ("", None, "-") else str(topo),
        "problem": str(row["problem"]),
        "inject": {str(k): str(v) for k, v in (row.get("inject") or {}).items()},
    }


def _rank_candidate(dev: dict, cand: dict) -> tuple[int, int, str]:
    """Lower is better: prefer different scenario, then topo, then stable id."""
    same_scenario = 1 if cand["scenario"] == dev["scenario"] else 0
    same_topo = (
        1 if (cand.get("topo_size") or "") == (dev.get("topo_size") or "") else 0
    )
    key = f"{cand['scenario']}|{cand.get('topo_size') or ''}|{benchmark_row_fingerprint(cand)}"
    return (same_scenario, same_topo, key)


def select_heldout_cases(
    *,
    dev_cases: list[dict],
    full_cases: list[dict],
    heldout_seed: int = HELDOUT_SEED,
) -> tuple[list[dict], list[str]]:
    """Return (test_rows, fallback_problems)."""
    full_by_problem: dict[str, list[dict]] = defaultdict(list)
    for row in full_cases:
        full_by_problem[str(row["problem"])].append(_normalize_row(row))

    test_rows: list[dict] = []
    fallbacks: list[str] = []

    for dev_raw in dev_cases:
        dev = _normalize_row(dev_raw)
        problem = dev["problem"]
        dev_fp = benchmark_row_fingerprint(dev)
        candidates = [
            c
            for c in full_by_problem.get(problem, [])
            if benchmark_row_fingerprint(c) != dev_fp
        ]
        if candidates:
            candidates.sort(key=lambda c: _rank_candidate(dev, c))
            test_rows.append(candidates[0])
            continue

        # No alternate in the full matrix: same scenario, new inject seed(s).
        topo = "" if not dev.get("topo_size") else str(dev["topo_size"])
        alt = None
        for seed in range(heldout_seed, heldout_seed + 32):
            inject = resolve_inject_params(problem, dev["scenario"], topo, seed=seed)
            validate_benchmark_case(dev["scenario"], problem, inject, topo)
            candidate = {
                "scenario": dev["scenario"],
                "topo_size": dev.get("topo_size"),
                "problem": problem,
                "inject": {str(k): str(v) for k, v in inject.items()},
            }
            if benchmark_row_fingerprint(candidate) != dev_fp:
                alt = candidate
                break
        if alt is None:
            # Last resort: flip host_name among lab devices when inject is deterministic.
            from nika.net_env.net_env_pool import get_net_env_instance

            env = get_net_env_instance(dev["scenario"])
            machines = sorted(env.lab.machines.keys()) if env.lab else []
            base_inject = dict(dev["inject"])
            host_key = next(
                (k for k in ("host_name", "attacker_device") if k in base_inject),
                None,
            )
            if host_key is None or not machines:
                raise RuntimeError(
                    f"Cannot synthesize held-out instance for {problem!r} "
                    f"on {dev['scenario']!r}"
                )
            for machine in machines:
                if str(machine) == str(base_inject[host_key]):
                    continue
                trial = dict(base_inject)
                trial[host_key] = str(machine)
                validate_benchmark_case(dev["scenario"], problem, trial, topo)
                candidate = {
                    "scenario": dev["scenario"],
                    "topo_size": dev.get("topo_size"),
                    "problem": problem,
                    "inject": trial,
                }
                if benchmark_row_fingerprint(candidate) != dev_fp:
                    alt = candidate
                    break
        if alt is None:
            raise RuntimeError(
                f"Held-out fallback for {problem!r} still matches Dev fingerprint"
            )
        test_rows.append(alt)
        fallbacks.append(problem)

    verify_dev_test_isolation(dev_cases=dev_cases, test_cases=test_rows)
    return test_rows, fallbacks


def generate_release_splits(
    *,
    version: str = DEFAULT_RELEASE_VERSION,
    selected_path: Path | None = None,
    full_path: Path | None = None,
    out_dir: Path | None = None,
) -> Path:
    selected_path = selected_path or (BENCHMARK_DIR / "benchmark_selected.yaml")
    full_path = full_path or (BENCHMARK_DIR / "benchmark_full.yaml")
    dest = out_dir or (releases_dir() / version)
    dest.mkdir(parents=True, exist_ok=True)

    # Dev: snapshot of curated selected suite.
    dev_path = dest / "dev.yaml"
    shutil.copyfile(selected_path, dev_path)
    # Drop legacy single-file name if present.
    legacy = dest / "cases.yaml"
    if legacy.is_file() and legacy.resolve() != dev_path.resolve():
        legacy.unlink()

    dev_cases = load_benchmark_yaml(dev_path)
    full_cases = load_benchmark_yaml(full_path)
    test_cases, fallbacks = select_heldout_cases(
        dev_cases=dev_cases, full_cases=full_cases
    )

    test_path = dest / "test.yaml"
    import hashlib

    dev_sha = hashlib.sha256(dev_path.read_bytes()).hexdigest()
    test_payload = {"seed": HELDOUT_SEED, "cases": test_cases}
    test_path.write_text(
        yaml.safe_dump(test_payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    test_sha = hashlib.sha256(test_path.read_bytes()).hexdigest()

    scenarios = {row["scenario"] for row in dev_cases} | {
        row["scenario"] for row in test_cases
    }
    problems = {row["problem"] for row in dev_cases}
    pins = build_scenario_problem_pins(scenarios, problems)
    images = {"required": collect_images_for_scenarios(scenarios)}
    splits = {
        "dev": {
            "cases_file": "dev.yaml",
            "case_count": len(dev_cases),
            "cases_sha256": dev_sha,
        },
        "test": {
            "cases_file": "test.yaml",
            "case_count": len(test_cases),
            "cases_sha256": test_sha,
        },
    }
    digest = write_release_manifest(
        dest,
        version=version,
        splits=splits,
        defaults=dict(DEFAULTS_V1),
        scoring=dict(SCORING_V1),
        tools=dict(TOOLS_V1),
        resources=dict(RESOURCES_V1),
        images=images,
        scenario_problem_pin=pins,
    )

    print(f"Wrote {dest}")
    print(f"  dev:  {len(dev_cases)} cases sha256={dev_sha[:12]}…")
    print(f"  test: {len(test_cases)} cases sha256={test_sha[:12]}…")
    print(f"  benchmark_digest={digest}")
    if fallbacks:
        print(f"  seed={HELDOUT_SEED} inject fallbacks: {', '.join(fallbacks)}")
    else:
        print("  no inject-seed fallbacks needed")
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        default=DEFAULT_RELEASE_VERSION,
        help=f"Release version directory (default: {DEFAULT_RELEASE_VERSION})",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Optional explicit output directory (default: benchmark/releases/<version>)",
    )
    args = parser.parse_args(argv)
    generate_release_splits(version=args.version, out_dir=args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
