"""Plot scenario × failure coverage from working benchmark YAML matrices.

Reads ``benchmark_full.yaml`` and ``benchmark_selected.yaml``, then writes a
high-DPI PNG under ``assets/images/`` for the docs coverage matrix section.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import yaml
from matplotlib.colors import ListedColormap
from matplotlib.patches import Patch

from nika.config import BENCHMARK_DIR, REPO_ROOT

EMPTY = 0
FULL_ONLY = 1
SELECTED = 2

DEFAULT_OUT = REPO_ROOT / "assets" / "images" / "benchmark_coverage_matrix.png"


def _load_pairs(path: Path) -> set[tuple[str, str]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = data.get("cases") or []
    return {(str(row["scenario"]), str(row["problem"])) for row in cases}


def build_matrix(
    full_path: Path,
    selected_path: Path,
) -> tuple[list[str], list[str], np.ndarray]:
    full_pairs = _load_pairs(full_path)
    selected_pairs = _load_pairs(selected_path)

    scenarios = sorted({s for s, _ in full_pairs} | {s for s, _ in selected_pairs})
    problems = sorted({p for _, p in full_pairs} | {p for _, p in selected_pairs})

    grid = np.zeros((len(problems), len(scenarios)), dtype=int)
    scenario_index = {name: i for i, name in enumerate(scenarios)}
    problem_index = {name: i for i, name in enumerate(problems)}

    for scenario, problem in full_pairs:
        grid[problem_index[problem], scenario_index[scenario]] = FULL_ONLY
    for scenario, problem in selected_pairs:
        grid[problem_index[problem], scenario_index[scenario]] = SELECTED

    return scenarios, problems, grid


def plot_matrix(
    scenarios: list[str],
    problems: list[str],
    grid: np.ndarray,
    out_path: Path,
    *,
    dpi: int = 200,
) -> None:
    n_problems, n_scenarios = grid.shape
    # Wide enough for full scenario labels; tall enough for every failure name.
    fig_w = max(10.0, 1.1 * n_scenarios + 4.0)
    fig_h = max(12.0, 0.28 * n_problems + 2.5)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))

    cmap = ListedColormap(["#f5f5f5", "#6baed6", "#fdae6b"])
    ax.imshow(
        grid,
        aspect="auto",
        cmap=cmap,
        vmin=EMPTY,
        vmax=SELECTED,
        interpolation="nearest",
    )

    ax.set_xticks(np.arange(n_scenarios))
    ax.set_yticks(np.arange(n_problems))
    ax.set_xticklabels(scenarios, rotation=45, ha="right", fontsize=9)
    ax.set_yticklabels(problems, fontsize=8)

    ax.set_xticks(np.arange(-0.5, n_scenarios, 1), minor=True)
    ax.set_yticks(np.arange(-0.5, n_problems, 1), minor=True)
    ax.grid(which="minor", color="#ffffff", linestyle="-", linewidth=0.8)
    ax.tick_params(which="minor", bottom=False, left=False)

    ax.set_xlabel("Scenario")
    ax.set_ylabel("Failure")
    ax.set_title("Coverage matrix (scenario × failure)")

    legend = [
        Patch(
            facecolor="#fdae6b", edgecolor="#cccccc", label="Selected / release 0.1.0"
        ),
        Patch(facecolor="#6baed6", edgecolor="#cccccc", label="Full matrix only"),
        Patch(facecolor="#f5f5f5", edgecolor="#cccccc", label="Not tag-compatible"),
    ]
    ax.legend(
        handles=legend, loc="upper left", bbox_to_anchor=(1.02, 1.0), frameon=True
    )

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full",
        type=Path,
        default=BENCHMARK_DIR / "benchmark_full.yaml",
        help="Path to benchmark_full.yaml",
    )
    parser.add_argument(
        "--selected",
        type=Path,
        default=BENCHMARK_DIR / "benchmark_selected.yaml",
        help="Path to benchmark_selected.yaml",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUT,
        help="Output PNG path",
    )
    parser.add_argument("--dpi", type=int, default=200, help="Output DPI")
    args = parser.parse_args()

    scenarios, problems, grid = build_matrix(args.full, args.selected)
    plot_matrix(scenarios, problems, grid, args.output, dpi=args.dpi)
    print(
        f"Wrote {args.output} "
        f"({len(problems)} failures × {len(scenarios)} scenarios, dpi={args.dpi})"
    )


if __name__ == "__main__":
    main()
