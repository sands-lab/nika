"""Render a ``SummaryReport`` as terminal tables with inline score bars.

``rich`` ships with Typer, so this adds no dependency. Bars are drawn from
block characters in a normal table column rather than by a plotting library:
they survive piping, redirection, and SSH, and degrade to plain text when the
output is not a terminal.
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from nika.workflows.eval.report import Breakdown, SummaryReport

_BAR_WIDTH = 12
_BAR_FULL = "█"
# Eighth-width blocks so a bar can end part-way through a cell.
_BAR_PARTIALS = ("", "▏", "▎", "▍", "▌", "▋", "▊", "▉")

_METRIC_HEADERS = {
    "rca_f1": "rca_f1",
    "localization_f1": "loc_f1",
    "detection_score": "det",
}

# Score bands for colouring. Ordered high to low.
_BANDS = ((0.60, "green"), (0.30, "yellow"), (0.0, "red"))


def _bar(value: float, *, width: int = _BAR_WIDTH) -> str:
    """A 0..1 score as a block bar, rounded to the nearest eighth of a cell."""
    fraction = max(0.0, min(1.0, float(value)))
    eighths = round(fraction * width * 8)
    full, remainder = divmod(eighths, 8)
    return (_BAR_FULL * full + _BAR_PARTIALS[remainder]).ljust(width)


def _band(value: float) -> str:
    for threshold, colour in _BANDS:
        if value >= threshold:
            return colour
    return "red"


def _score_cell(value: float) -> str:
    return f"[{_band(value)}]{value:.3f}[/]"


def _bar_cell(value: float) -> str:
    return f"[{_band(value)}]{_bar(value)}[/]"


def _render_headline(console: Console, report: SummaryReport) -> None:
    table = Table(
        title="NIKA benchmark summary",
        title_style="bold",
        show_edge=True,
        header_style="bold",
    )
    table.add_column("metric")
    table.add_column("score", justify="right")
    table.add_column("", no_wrap=True)

    for attr, label in (
        ("mean_rca_f1", "rca_f1"),
        ("mean_localization_f1", "localization_f1"),
        ("mean_detection_score", "detection_score"),
    ):
        value = float(getattr(report, attr))
        marker = "  [bold](primary)[/]" if label == report.primary_metric else ""
        table.add_row(f"{label}{marker}", _score_cell(value), _bar_cell(value))
    console.print(table)

    basis = (
        f"leaderboard basis: missing trials count as 0 over "
        f"{report.n_trials_expected} expected"
        if report.expected_is_known
        else f"basis: {report.n_trials_present} trial(s) found; "
        "expected count unknown, so nothing is counted as missing"
    )
    console.print(f"[dim]{basis}[/]")


def _render_run_health(console: Console, report: SummaryReport) -> None:
    missing = max(report.n_trials_expected - report.n_trials_present, 0)
    parts = [
        f"trials {report.n_trials_present}/{report.n_trials_expected}",
        f"success {report.n_success}",
    ]
    if report.n_agent_failed:
        parts.append(f"[yellow]agent_failed {report.n_agent_failed}[/]")
    if missing:
        parts.append(f"[red]missing {missing}[/]")
    if report.n_unreadable:
        parts.append(f"[red]unreadable {report.n_unreadable}[/]")

    identity = []
    if report.agent_types:
        identity.append("/".join(report.agent_types))
    if report.models:
        identity.append("/".join(report.models))
    if identity:
        parts.append("[dim]" + "  ".join(identity) + "[/]")

    console.print("  ".join(parts))

    if report.n_agent_failed:
        console.print(
            f"[dim]{report.n_agent_failed} agent_failed trial(s) scored as 0.0 "
            "(their eval_metrics.json holds -1.0 sentinels).[/]"
        )


def _render_case_split(console: Console, report: SummaryReport) -> None:
    """Fault cases and healthy controls measure different things."""
    if not report.healthy_stats.n_trials:
        return
    table = Table(
        title="Fault cases vs. healthy controls",
        title_style="bold",
        header_style="bold",
    )
    table.add_column("case type")
    table.add_column("n", justify="right")
    table.add_column("rca_f1", justify="right")
    table.add_column("loc_f1", justify="right")
    table.add_column("det", justify="right")
    table.add_column("", no_wrap=True)

    fault = report.fault_stats
    if fault.n_trials:
        table.add_row(
            "fault injected",
            str(fault.n_trials),
            _score_cell(fault.rca_f1),
            _score_cell(fault.localization_f1),
            _score_cell(fault.detection_score),
            _bar_cell(fault.detection_score),
        )
    else:
        # A healthy-only run has nothing to score against.
        table.add_row(
            "fault injected", "0", "[dim]n/a[/]", "[dim]n/a[/]", "[dim]n/a[/]", ""
        )
    healthy = report.healthy_stats
    # RCA/localization have no ground truth to score against with no fault.
    table.add_row(
        "healthy (no fault)",
        str(healthy.n_trials),
        "[dim]n/a[/]",
        "[dim]n/a[/]",
        _score_cell(healthy.detection_score),
        _bar_cell(healthy.detection_score),
    )
    console.print(table)
    console.print(
        "[dim]healthy 'det' is the rate of correctly reporting no anomaly; "
        "a gap below the fault-case rate means over-reporting.[/]"
    )


def _render_breakdown(
    console: Console,
    breakdown: Breakdown,
    *,
    metric: str,
    top: int | None,
) -> None:
    if not breakdown.rows:
        return
    header = _METRIC_HEADERS.get(metric, metric)
    table = Table(
        title=f"Fault cases by {breakdown.label} (sorted by {header})",
        title_style="bold",
        header_style="bold",
    )
    table.add_column(breakdown.label, overflow="fold", min_width=22, ratio=2)
    table.add_column("n", justify="right")
    table.add_column("fail", justify="right")
    table.add_column("rca_f1", justify="right")
    table.add_column("loc_f1", justify="right")
    table.add_column("det", justify="right")
    table.add_column(header, no_wrap=True)

    rows = breakdown.rows if top is None else breakdown.rows[:top]
    for row in rows:
        stats = row.stats
        table.add_row(
            row.key,
            str(stats.n_trials),
            str(stats.n_agent_failed) if stats.n_agent_failed else "[dim]0[/]",
            _score_cell(stats.rca_f1),
            _score_cell(stats.localization_f1),
            _score_cell(stats.detection_score),
            _bar_cell(stats.value(metric)),
        )
    console.print(table)
    hidden = len(breakdown.rows) - len(rows)
    if hidden > 0:
        console.print(
            f"[dim]{hidden} further {breakdown.label} row(s) hidden by --top; "
            "pass --top 0 to show all.[/]"
        )


def _render_confusion(console: Console, report: SummaryReport, *, top: int) -> None:
    if not report.confusion:
        return
    shown = report.confusion[:top]
    table = Table(
        title=f"Top RCA confusions (ground truth -> predicted, top {len(shown)})",
        title_style="bold",
        header_style="bold",
    )
    table.add_column("ground truth", overflow="fold", ratio=1)
    table.add_column("predicted", overflow="fold", ratio=1)
    table.add_column("count", justify="right")
    for row in shown:
        style = "green" if row.gt == row.predicted else "red"
        table.add_row(row.gt, f"[{style}]{row.predicted}[/]", str(row.count))
    console.print(table)
    if report.n_missing_prediction:
        console.print(
            f"[dim]{report.n_missing_prediction} trial(s) submitted no root cause.[/]"
        )


def _render_cost(console: Console, report: SummaryReport) -> None:
    tokens = report.token_totals
    steps = report.steps_totals
    if not tokens and not steps:
        return
    fields = [
        ("in_tokens", tokens.get("in_tokens")),
        ("out_tokens", tokens.get("out_tokens")),
        ("steps", steps.get("steps")),
        ("tool_calls", steps.get("tool_calls")),
        ("tool_errors", steps.get("tool_errors")),
    ]
    rendered = "  ".join(
        f"{name} {value:,}" for name, value in fields if value is not None
    )
    if rendered:
        console.print(f"[dim]totals: {rendered}[/]")


def render_summary_report(
    report: SummaryReport,
    *,
    metric: str,
    top: int | None = 15,
    confusion_top: int = 10,
    console: Console | None = None,
) -> None:
    """Print ``report`` to the terminal.

    ``top`` limits rows per breakdown table (``None`` shows all). Colour and
    box drawing follow ``rich`` console detection, so piped output stays plain.
    """
    out = console or Console()
    out.print()
    _render_headline(out, report)
    _render_run_health(out, report)
    out.print()
    _render_case_split(out, report)
    for breakdown in report.breakdowns:
        out.print()
        _render_breakdown(out, breakdown, metric=metric, top=top)
    if report.confusion:
        out.print()
        _render_confusion(out, report, top=confusion_top)
    out.print()
    _render_cost(out, report)
    if report.empty_metric_families:
        out.print(
            "[dim]not scored (no data): "
            + ", ".join(report.empty_metric_families)
            + "[/]"
        )
