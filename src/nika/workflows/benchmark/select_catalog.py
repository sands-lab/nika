"""Select a compact benchmark subset using coverage-guided optimization."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

from nika.config import BENCHMARK_DIR
from nika.workflows.benchmark.candidate_context import collapse_candidates
from nika.workflows.benchmark.coverage_report import build_coverage_report
from nika.workflows.benchmark.load_config import load_candidate_catalog
from nika.workflows.benchmark.pool_audit import eligible_candidates
from nika.workflows.benchmark.selection import select_benchmark_cases

DEFAULT_POOL = BENCHMARK_DIR / "working" / "pool"
DEFAULT_OUTPUT = BENCHMARK_DIR / "working" / "cases.yaml"


def write_selected_catalog(
    *,
    pool: Path | None = None,
    output: Path | None = None,
    seed: int = 42,
    skip_audit: bool = False,
) -> dict[str, Any]:
    """Select cases from the candidate pool and write ``working/cases.yaml``."""
    pool_path = pool or DEFAULT_POOL
    output_path = output or DEFAULT_OUTPUT
    raw_rows = load_candidate_catalog(pool_path)
    candidates = (
        collapse_candidates(raw_rows)
        if skip_audit
        else eligible_candidates(str(pool_path))
    )

    cases, _ = select_benchmark_cases(candidates, seed=seed)
    coverage = build_coverage_report(
        selected_cases=cases,
        pool_candidates=candidates,
    )
    if not coverage["selection_contract"]["passed"]:
        raise ValueError(
            f"Selected cases violate benchmark contract: {coverage['selection_contract']}"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        yaml.safe_dump(
            {
                "selection": {
                    "seed": seed,
                },
                "cases": cases,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    coverage["output"] = str(output_path)
    return coverage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pool",
        type=Path,
        default=DEFAULT_POOL,
        help="Candidate pool directory",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Selected flat cases YAML output path",
    )
    parser.add_argument("--seed", type=int, default=42, help="Selection random seed")
    parser.add_argument(
        "--skip-audit",
        action="store_true",
        help="Skip pool audit gate (not recommended)",
    )
    args = parser.parse_args(argv)

    coverage = write_selected_catalog(
        pool=args.pool,
        output=args.output,
        seed=args.seed,
        skip_audit=args.skip_audit,
    )
    summary = coverage["summary"]
    print("selected benchmark")
    for key, value in summary.items():
        print(f"  {key}: {value}")
    print(f"comparison_vs_baseline: {coverage['comparison_vs_baseline']}")
    print(f"Wrote {coverage['output']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
