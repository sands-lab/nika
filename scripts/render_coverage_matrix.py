"""Render failure × scenario coverage as HTML tables for docs.

Compatibility is scenario-scoped (and ISP deploy-variant scoped), not whether
``benchmark_full.yaml`` sampled that column. Release membership overlays
``benchmark/releases/0.1.0`` (dev + test).

Tables use two real header rows (scenario, then config). Cells: blank =
incompatible, ``○`` = compatible, ``●`` = compatible and in release.

Usage::

    uv run python scripts/render_coverage_matrix.py
    uv run python scripts/render_coverage_matrix.py --write-docs
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from nika.config import BENCHMARK_DIR, REPO_ROOT
from nika.net_env.net_env_pool import coverage_columns, parse_column
from nika.problems.registry import compatible, list_avail_problem_instances
from nika.problems.base import FailureDomain
from nika.workflows.benchmark.isp_options import ISP_SCENARIO, isp_column_suffix
from nika.workflows.benchmark.healthy import is_healthy_case
from nika.workflows.benchmark.load_config import normalize_benchmark_row

DOCS_PATH = REPO_ROOT / "docs" / "benchmark-configuration.md"
SECTION_START = "## Coverage matrix (scenario × failure)"
SECTION_END = "## Regeneration"
DEFAULT_RELEASE = BENCHMARK_DIR / "releases" / "0.1.0"

# Same order as docs/failures.md.
DOMAIN_ORDER: tuple[FailureDomain, ...] = (
    FailureDomain.LINK_INTERFACE,
    FailureDomain.ROUTING_CONTROL_PLANE,
    FailureDomain.FORWARDING_ENCAPSULATION_POLICY,
    FailureDomain.SERVICE_NETWORKING,
    FailureDomain.MANAGEMENT_ORCHESTRATION_PLANE,
    FailureDomain.ADDRESSING_NEIGHBOR_NAMING,
    FailureDomain.ENDPOINT_APPLICATION,
    FailureDomain.TRAFFIC_QUEUEING_RESOURCE,
    FailureDomain.SECURITY,
)

DOMAIN_LABEL: dict[FailureDomain, str] = {
    FailureDomain.LINK_INTERFACE: "Link & Interface",
    FailureDomain.ROUTING_CONTROL_PLANE: "Routing & Control Plane",
    FailureDomain.FORWARDING_ENCAPSULATION_POLICY: "Forwarding, Encapsulation & Policy",
    FailureDomain.SERVICE_NETWORKING: "Service Networking",
    FailureDomain.MANAGEMENT_ORCHESTRATION_PLANE: "Management & Orchestration Plane",
    FailureDomain.ADDRESSING_NEIGHBOR_NAMING: "Addressing, Neighbor & Naming",
    FailureDomain.ENDPOINT_APPLICATION: "Endpoint & Application",
    FailureDomain.TRAFFIC_QUEUEING_RESOURCE: "Traffic, Queueing & Resource",
    FailureDomain.SECURITY: "Security",
}

SCENARIO_DISPLAY: dict[str, str] = {
    "campus_lan": "campus",
    "dc_clos": "clos",
    "enterprise_branch": "enterprise",
    "k8s_lab": "k8s",
    "llmd_lab": "llmd",
    "p4_dc_fabric": "p4_dc_fabric",
    "sdn_l3_clos": "sdn_l3_clos",
    "min3clos": "min3clos",
    "isp": "isp",
}

MARK_COMPATIBLE = "○"
MARK_RELEASE = "●"


def _column_label(row: dict) -> str:
    scenario = str(row["scenario"])
    if scenario == ISP_SCENARIO:
        return (
            f"{ISP_SCENARIO}/"
            f"{isp_column_suffix(topo=row.get('topo'), igp=row.get('igp'), bgp_mode=row.get('bgp_mode'))}"
        )
    return scenario


def _load_pairs(path: Path) -> set[tuple[str, str]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    pairs: set[tuple[str, str]] = set()
    for raw in data.get("cases") or []:
        try:
            row = normalize_benchmark_row(raw)
        except ValueError:
            # Frozen releases may reference retired scenario IDs.
            continue
        if is_healthy_case(row.get("problem")):
            continue
        pairs.add((_column_label(row), str(row["problem"])))
    return pairs


def _load_release_pairs(release_dir: Path) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for name in ("dev.yaml", "test.yaml"):
        path = release_dir / name
        if path.is_file():
            pairs |= _load_pairs(path)
    return pairs


def _problem_domain(name: str) -> FailureDomain | None:
    cls = list_avail_problem_instances().get(name)
    if cls is None or cls.META is None:
        return None
    domain = cls.META.failure_domain
    if isinstance(domain, FailureDomain):
        return domain
    try:
        return FailureDomain(domain)
    except ValueError:
        return None


def _domain_rank(domain: FailureDomain | None) -> int:
    if domain is None:
        return len(DOMAIN_ORDER)
    try:
        return DOMAIN_ORDER.index(domain)
    except ValueError:
        return len(DOMAIN_ORDER)


def _scenario_groups(
    columns: list[str],
) -> list[tuple[str, list[tuple[str, str | None]]]]:
    """Group columns under a display scenario name for the two-row header."""
    groups: list[tuple[str, list[tuple[str, str | None]]]] = []
    for column in columns:
        scenario, config = parse_column(column)
        display = SCENARIO_DISPLAY.get(scenario, scenario)
        if (
            groups
            and groups[-1][0] == display
            and config is not None
            and all(cfg is not None for _col, cfg in groups[-1][1])
        ):
            groups[-1][1].append((column, config))
        else:
            groups.append((display, [(column, config)]))
    return groups


def _cell_mark(
    column: str,
    problem: str,
    *,
    release_pairs: set[tuple[str, str]],
) -> str:
    if not compatible(problem, column):
        return ""
    if (column, problem) in release_pairs:
        return MARK_RELEASE
    return MARK_COMPATIBLE


def _render_html_table(
    *,
    columns: list[str],
    problems: list[str],
    release_pairs: set[tuple[str, str]],
) -> str:
    groups = _scenario_groups(columns)
    flat_columns = [col for _name, members in groups for col, _cfg in members]

    lines: list[str] = ["<table>", "<thead>", "<tr>", '<th rowspan="2">Failure</th>']
    for display, members in groups:
        if len(members) == 1 and members[0][1] is None:
            lines.append(f'<th rowspan="2">{display}</th>')
        else:
            lines.append(f'<th colspan="{len(members)}">{display}</th>')
    lines.append("</tr>")
    lines.append("<tr>")
    for _display, members in groups:
        for _column, config in members:
            if config is None:
                continue
            lines.append(f"<th>{config}</th>")
    lines.append("</tr>")
    lines.append("</thead>")
    lines.append("<tbody>")
    for problem in problems:
        lines.append("<tr>")
        lines.append(f"<td><code>{problem}</code></td>")
        for column in flat_columns:
            mark = _cell_mark(
                column,
                problem,
                release_pairs=release_pairs,
            )
            lines.append(f'<td align="center">{mark}</td>')
        lines.append("</tr>")
    lines.append("</tbody>")
    lines.append("</table>")
    return "\n".join(lines)


def render_tables(*, release_dir: Path) -> str:
    release_pairs = _load_release_pairs(release_dir)
    columns = coverage_columns()
    problems = sorted(
        list_avail_problem_instances(),
        key=lambda name: (_domain_rank(_problem_domain(name)), name),
    )

    lines: list[str] = []
    for domain in DOMAIN_ORDER:
        domain_problems = [p for p in problems if _problem_domain(p) == domain]
        if not domain_problems:
            continue
        lines.append(f"### {DOMAIN_LABEL[domain]}")
        lines.append("")
        lines.append(
            _render_html_table(
                columns=columns,
                problems=domain_problems,
                release_pairs=release_pairs,
            )
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_section(*, release_dir: Path) -> str:
    tables = render_tables(release_dir=release_dir)
    release_name = release_dir.name
    return (
        f"{SECTION_START}\n"
        "\n"
        "Cells mark **capability**, not whether `benchmark_full.yaml` sampled "
        "that config. A failure is compatible when its tags and deploy "
        "constraints match the scenario and ISP `topo`/`igp`/`bgp_mode` "
        "profile. `benchmark_full.yaml` remains a "
        "one-config-per-failure runnable sample. Release membership comes from "
        f"`benchmark/releases/{release_name}/` (dev + test).\n"
        "\n"
        "Each table has two header rows: scenario, then config (when the "
        "scenario has more than one). Cells: blank = incompatible, `○` = "
        f"compatible, `●` = compatible and in release `{release_name}`. "
        "Tables are split by failure subsystem.\n"
        "\n"
        "Regenerate after registry or TAGS changes:\n"
        "\n"
        "```shell\n"
        "uv run python scripts/render_coverage_matrix.py --write-docs\n"
        "```\n"
        "\n"
        f"{tables}"
        "\n"
        "When you add, remove, or retarget cases (new failure, new scenario, or "
        "a `TAGS` / `COMPATIBLE_COLUMNS` / registry change that changes "
        "compatibility), refresh this "
        "section in the same change.\n"
        "\n"
    )


def write_docs(*, docs_path: Path, release_dir: Path) -> None:
    text = docs_path.read_text(encoding="utf-8")
    start = text.index(SECTION_START)
    end = text.index(SECTION_END, start)
    section = render_section(release_dir=release_dir)
    docs_path.write_text(text[:start] + section + text[end:], encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--release-dir",
        type=Path,
        default=DEFAULT_RELEASE,
        help="Release directory with dev.yaml and test.yaml",
    )
    parser.add_argument(
        "--write-docs",
        action="store_true",
        help=f"Replace the coverage section in {DOCS_PATH}",
    )
    args = parser.parse_args()
    if args.write_docs:
        write_docs(docs_path=DOCS_PATH, release_dir=args.release_dir)
        print(f"Updated {DOCS_PATH}")
        return
    print(render_section(release_dir=args.release_dir), end="")


if __name__ == "__main__":
    main()
