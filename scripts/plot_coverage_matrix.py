"""Plot scenario × failure compatibility from working benchmark YAML matrices.

Reads ``benchmark_full.yaml`` and ``benchmark_selected.yaml``, then writes a
failure-level matrix under ``assets/images/``.

Columns are ``scenario`` or ``scenario/workload`` (topo size collapsed). Cells
primarily encode tag compatibility; release membership is a secondary overlay.
Failure rows follow the six-category taxonomy in ``docs/failures.md``.
Regenerate this image whenever working YAML cases change.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import yaml
from matplotlib.lines import Line2D

from nika.config import BENCHMARK_DIR, REPO_ROOT
from nika.problems.prob_pool import list_avail_problem_instances
from nika.problems.problem_base import RootCauseCategory

INCOMPATIBLE = 0
COMPATIBLE = 1
IN_RELEASE = 2

COLOR_INCOMPATIBLE = "#ffffff"
COLOR_COMPATIBLE = "#cfe8f8"
COLOR_RELEASE = "#0969da"
COLOR_TEXT = "#24292f"
COLOR_MUTED = "#57606a"
COLOR_GRID = "#d8dee4"
COLOR_GROUP = "#d0d7de"

DEFAULT_OUT = REPO_ROOT / "assets" / "images" / "benchmark_coverage_matrix.png"
DEFAULT_RELEASE = "0.1.0"

# Same order as the category table in docs/failures.md (not enum declaration order).
CATEGORY_ORDER: tuple[RootCauseCategory, ...] = (
    RootCauseCategory.LINK_FAILURE,
    RootCauseCategory.END_HOST_FAILURE,
    RootCauseCategory.NETWORK_NODE_ERROR,
    RootCauseCategory.MISCONFIGURATION,
    RootCauseCategory.RESOURCE_CONTENTION,
    RootCauseCategory.NETWORK_UNDER_ATTACK,
)

CATEGORY_LABEL: dict[RootCauseCategory, str] = {
    RootCauseCategory.LINK_FAILURE: "Link failures",
    RootCauseCategory.END_HOST_FAILURE: "End-host failures",
    RootCauseCategory.NETWORK_NODE_ERROR: "Network node errors",
    RootCauseCategory.MISCONFIGURATION: "Misconfigurations",
    RootCauseCategory.RESOURCE_CONTENTION: "Resource contention",
    RootCauseCategory.NETWORK_UNDER_ATTACK: "Network under attack",
}


def _configure_style() -> None:
    mpl.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["DejaVu Sans", "Arial", "Helvetica", "sans-serif"],
            "axes.linewidth": 0.6,
            "axes.labelsize": 10,
            "axes.titlesize": 11,
            "xtick.labelsize": 8,
            "ytick.labelsize": 7.5,
            "legend.fontsize": 8,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "savefig.facecolor": "white",
            "figure.facecolor": "white",
        }
    )


def _column_label(scenario: str, workload: str | None) -> str:
    """Return scenario/workload, with topo size collapsed.

    The slash remains an internal delimiter and is replaced for display.
    """
    if workload:
        return f"{scenario}/{workload}"
    return scenario


def _load_pairs(path: Path) -> set[tuple[str, str]]:
    """Return (column_label, problem) pairs, ignoring topo_size."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = data.get("cases") or []
    pairs: set[tuple[str, str]] = set()
    for row in cases:
        workload = row.get("workload")
        label = _column_label(
            str(row["scenario"]),
            str(workload) if workload is not None else None,
        )
        pairs.add((label, str(row["problem"])))
    return pairs


def _problem_category(name: str) -> RootCauseCategory | None:
    cls = list_avail_problem_instances().get(name)
    if cls is None or cls.META is None:
        return None
    category = cls.META.root_cause_category
    if isinstance(category, RootCauseCategory):
        return category
    try:
        return RootCauseCategory(category)
    except ValueError:
        return None


def _category_rank(category: RootCauseCategory | None) -> int:
    if category is None:
        return len(CATEGORY_ORDER)
    try:
        return CATEGORY_ORDER.index(category)
    except ValueError:
        return len(CATEGORY_ORDER)


def sort_problems_by_category(problems: list[str]) -> list[str]:
    """Order failures by taxonomy category, then alphabetically within category."""
    return sorted(
        problems,
        key=lambda name: (_category_rank(_problem_category(name)), name),
    )


def _parse_column(column: str) -> tuple[str, str | None]:
    if "/" in column:
        scenario, workload = column.split("/", 1)
        return scenario, workload
    return column, None


def _scenario_groups(
    columns: list[str],
) -> list[tuple[str, int, int]]:
    """Return (scenario, start, end) inclusive spans for contiguous columns."""
    if not columns:
        return []
    groups: list[tuple[str, int, int]] = []
    start = 0
    current, _ = _parse_column(columns[0])
    for idx in range(1, len(columns)):
        scenario, _ = _parse_column(columns[idx])
        if scenario != current:
            groups.append((current, start, idx - 1))
            start = idx
            current = scenario
    groups.append((current, start, len(columns) - 1))
    return groups


def _column_positions(columns: list[str], group_gap: float = 0.3) -> np.ndarray:
    """Return column centers with a small gap between scenario groups."""
    positions = np.arange(len(columns), dtype=float)
    for _scenario, _start, end in _scenario_groups(columns)[:-1]:
        positions[end + 1 :] += group_gap
    return positions


def _display_scenario(scenario: str, column_count: int) -> str:
    """Wrap long single-column scenario names at underscores."""
    if column_count == 1 and "_" in scenario and len(scenario) > 6:
        return scenario.replace("_", "\n")
    return scenario


def _column_sort_key(column: str) -> tuple[str, str]:
    scenario, workload = _parse_column(column)
    return (scenario, workload or "")


def build_matrix(
    full_path: Path,
    selected_path: Path,
) -> tuple[list[str], list[str], np.ndarray]:
    full_pairs = _load_pairs(full_path)
    selected_pairs = _load_pairs(selected_path)

    columns = sorted(
        {c for c, _ in full_pairs} | {c for c, _ in selected_pairs},
        key=_column_sort_key,
    )
    problems = sort_problems_by_category(
        list({p for _, p in full_pairs} | {p for _, p in selected_pairs})
    )

    grid = np.zeros((len(problems), len(columns)), dtype=int)
    column_index = {name: i for i, name in enumerate(columns)}
    problem_index = {name: i for i, name in enumerate(problems)}

    for column, problem in full_pairs:
        grid[problem_index[problem], column_index[column]] = COMPATIBLE
    for column, problem in selected_pairs:
        grid[problem_index[problem], column_index[column]] = IN_RELEASE

    return columns, problems, grid


def _category_boundaries(
    problems: list[str],
) -> list[tuple[int, int, RootCauseCategory | None]]:
    """Return inclusive (start, end, category) spans for contiguous problem groups."""
    if not problems:
        return []
    spans: list[tuple[int, int, RootCauseCategory | None]] = []
    start = 0
    current = _problem_category(problems[0])
    for idx in range(1, len(problems)):
        category = _problem_category(problems[idx])
        if category != current:
            spans.append((start, idx - 1, current))
            start = idx
            current = category
    spans.append((start, len(problems) - 1, current))
    return spans


def plot_matrix(
    columns: list[str],
    problems: list[str],
    grid: np.ndarray,
    out_path: Path,
    *,
    release: str = DEFAULT_RELEASE,
    dpi: int = 150,
) -> None:
    """Render six stacked failure-category blocks with shared columns."""
    _configure_style()
    groups = _scenario_groups(columns)
    column_x = _column_positions(columns)
    x_limits = (column_x[0] - 0.45, column_x[-1] + 0.45)
    spans = _category_boundaries(problems)
    row_counts = [end - start + 1 for start, end, _ in spans]
    fig_height = max(14.4, 0.2 * len(problems) + 2.6)
    fig = plt.figure(figsize=(9.33, fig_height))
    outer_grid = fig.add_gridspec(
        2,
        1,
        height_ratios=[3.2, sum(row_counts)],
        hspace=0.02,
    )
    matrix_grid = outer_grid[1].subgridspec(
        len(spans),
        1,
        height_ratios=row_counts,
        hspace=0.58,
    )
    header = fig.add_subplot(outer_grid[0])
    axes = [fig.add_subplot(matrix_grid[index]) for index in range(len(spans))]

    header.set_xlim(*x_limits)
    header.set_ylim(0.0, 1.0)
    header.axis("off")
    for scenario, group_start, group_end in groups:
        start_x = column_x[group_start]
        end_x = column_x[group_end]
        midpoint = (start_x + end_x) / 2.0
        header.text(
            midpoint,
            0.67,
            _display_scenario(scenario, group_end - group_start + 1),
            ha="center",
            va="center",
            fontsize=6.3,
            fontweight="bold",
            color=COLOR_TEXT,
            linespacing=0.95,
        )
        header.plot(
            [start_x - 0.38, end_x + 0.38],
            [0.34, 0.34],
            color=COLOR_GROUP,
            linewidth=0.55,
        )
    for index, column in enumerate(columns):
        _scenario, workload = _parse_column(column)
        if workload:
            header.text(
                column_x[index],
                0.12,
                workload,
                ha="center",
                va="center",
                fontsize=5.5,
                color=COLOR_MUTED,
            )

    for ax, (start, end, category) in zip(axes, spans):
        block = grid[start : end + 1]
        block_problems = problems[start : end + 1]
        rows = np.arange(len(block_problems))

        ax.set_xlim(*x_limits)
        ax.set_ylim(len(block_problems) - 0.5, -0.5)
        ax.set_yticks(rows)
        ax.set_yticklabels(block_problems, color=COLOR_TEXT)
        ax.tick_params(axis="y", length=0, pad=7)

        for y in np.arange(0.5, len(block_problems), 1):
            ax.axhline(y, color=COLOR_GRID, linewidth=0.35, zorder=0)

        compatible_y, compatible_x = np.where(block == COMPATIBLE)
        release_y, release_x = np.where(block == IN_RELEASE)
        ax.scatter(
            column_x[compatible_x],
            compatible_y,
            s=26,
            marker="o",
            color=COLOR_COMPATIBLE,
            edgecolors="none",
            zorder=2,
        )
        ax.scatter(
            column_x[release_x],
            release_y,
            s=30,
            marker="o",
            color=COLOR_RELEASE,
            edgecolors="none",
            zorder=3,
        )

        label = "Other" if category is None else CATEGORY_LABEL[category]
        ax.set_title(
            label,
            loc="left",
            fontsize=9,
            fontweight="bold",
            color=COLOR_TEXT,
            pad=6,
        )
        for spine in ax.spines.values():
            spine.set_visible(False)

        ax.set_xticks([])

    fig.suptitle(
        "Scenario–failure compatibility",
        x=0.01,
        y=0.993,
        ha="left",
        fontsize=12,
        fontweight="bold",
        color=COLOR_TEXT,
    )
    legend = [
        Line2D(
            [0],
            [0],
            marker="o",
            linestyle="none",
            markerfacecolor=COLOR_COMPATIBLE,
            markeredgecolor="none",
            markersize=5,
            label="Compatible",
        ),
        Line2D(
            [0],
            [0],
            marker="o",
            linestyle="none",
            markerfacecolor=COLOR_RELEASE,
            markeredgecolor="none",
            markersize=5,
            label=f"In release {release}",
        ),
    ]
    fig.legend(
        handles=legend,
        loc="upper right",
        bbox_to_anchor=(0.99, 0.994),
        ncols=2,
        frameon=False,
        handletextpad=0.35,
        columnspacing=1.2,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.subplots_adjust(left=0.27, right=0.99, top=0.965, bottom=0.02)
    fig.savefig(out_path, dpi=dpi)
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
        help="Failure-level matrix PNG path",
    )
    parser.add_argument(
        "--release",
        default=DEFAULT_RELEASE,
        help="Release version shown in the legend (default: %(default)s)",
    )
    parser.add_argument("--dpi", type=int, default=150, help="Output DPI")
    args = parser.parse_args()

    scenarios, problems, grid = build_matrix(args.full, args.selected)
    plot_matrix(
        scenarios,
        problems,
        grid,
        args.output,
        release=args.release,
        dpi=args.dpi,
    )
    print(
        f"Wrote {args.output} "
        f"({len(problems)} failures × {len(scenarios)} columns, dpi={args.dpi})"
    )


if __name__ == "__main__":
    main()
