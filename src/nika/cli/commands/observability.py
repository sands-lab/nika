"""CLI: toggle monitoring of lab resources and benchmark progress."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import typer
from rich.console import Console
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text

observability_app = typer.Typer(
    help="Record lab CPU and memory per session and benchmark progress; view them in Grafana.",
)

_console = Console()


def _fail(exc: Exception) -> None:
    _console.print(f"❌ [bold red]Error:[/] {exc}")
    raise typer.Exit(code=1) from exc


def _link(url: str) -> Text:
    return Text(url, style=Style(link=url, color="cyan"))


def _home(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home) :] if path.startswith(home + "/") else path


def _time_left(expires_at: str | None) -> str:
    if not expires_at:
        return "?"
    try:
        left = datetime.fromisoformat(expires_at) - datetime.now(timezone.utc)
    except ValueError:
        return "?"
    seconds = int(left.total_seconds())
    if seconds <= 0:
        return "expired"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {rest // 60}m" if hours else f"{rest // 60}m"


def _status_panel(state: dict[str, Any]) -> Panel:
    on = state["on"]
    grid = Table.grid(padding=(0, 2))
    grid.add_column(no_wrap=True)
    grid.add_column()

    services = Text()
    for name, status in state["services"].items():
        if services:
            services.append("  ")
        icon = "✅" if status == "running" else "⚪" if status == "missing" else "⛔"
        services.append(f"{icon} {name}", style="" if status == "running" else "dim")
    grid.add_row("🐳 Services", services)
    if on:
        grid.add_row("📊 Grafana", _link(state["grafana"]))
        grid.add_row("🔥 Prometheus", _link(state["prometheus"]))
        grid.add_row(
            "🔑 Grafana admin",
            Text(f"password in {_home(state['grafana_password_file'])}", style="dim"),
        )
    grid.add_row("🕒 Sampling", f"every {state['scrape_interval']}")

    remote = state["remote_write"]
    if remote:
        cloud = Text("✅ pushing to ", style="green")
        cloud.append(urlparse(remote).hostname or remote, style="cyan")
    else:
        cloud = Text("⚪ off", style="dim")
    grid.add_row("🌐 Grafana Cloud", cloud)

    share = state["share"]
    if share.enabled:
        access = (
            f"{len(share.allowed_emails)} allowed email(s)"
            if share.allowed_emails
            else "anyone with the link"
        )
        links = Text(f"✅ per run, {share.ttl_days} days, {access}", style="green")
    else:
        links = Text("⚪ off", style="dim")
    grid.add_row("🔗 Run links", links)

    exporter = Text(
        "🟢 running" if state["exporter_running"] else "💤 idle",
        style="green" if state["exporter_running"] else "dim",
    )
    exporter.append(f"  ({state['exporters']} on this host)", style="dim")
    grid.add_row("🧩 Exporter", exporter)

    title = Text("📡 NIKA observability  ")
    title.append("● on" if on else "● off", style="bold green" if on else "bold red")
    return Panel(
        grid,
        title=title,
        title_align="left",
        border_style="green" if on else "red",
        expand=False,
    )


def _print_links(links: list[dict[str, Any]], *, title: str) -> None:
    """One block per run; links stay on one line so they copy intact."""
    _console.print(Text(title, style="bold"))
    for link in links:
        head = Text("  ")
        name = str(link.get("run_name") or link["run_id"])
        if link["live"]:
            head.append("● ", style="green")
            head.append(name, style="bold")
            head.append(f"  expires in {_time_left(link['expires_at'])}", style="green")
        else:
            head.append("○ ", style="dim")
            head.append(name, style="bold dim")
            ended = (
                "expired" if _time_left(link["expires_at"]) == "expired" else "closed"
            )
            head.append(f"  {ended}", style="dim")
        if link.get("total"):
            head.append(f"  {link['completed']}/{link['total']} trials", style="dim")
        if name != link["run_id"]:
            head.append(f"  run {link['run_id']}", style="dim")
        _console.print(head)
        url = Text("    ")
        url.append_text(
            _link(link["url"])
            if link["live"]
            else Text(link["url"], style="dim strike")
        )
        _console.print(url, soft_wrap=True)


@observability_app.command("on")
def observability_on() -> None:
    """Start cAdvisor, Prometheus, and Grafana; later labs and runs report automatically."""
    from nika.metrics.stack import observability_on, status

    with _console.status("Starting the monitoring stack…"):
        try:
            observability_on()
        except RuntimeError as exc:
            _fail(exc)
    _console.print("✅ [bold green]Observability on.[/]")
    _console.print(_status_panel(status()))


@observability_app.command("off")
def observability_off(
    purge: bool = typer.Option(
        False,
        "--purge",
        help="Also delete the recorded Prometheus history and Grafana state.",
    ),
) -> None:
    """Stop the monitoring stack, the session exporters, and shared run links."""
    from nika.metrics.stack import observability_off

    with _console.status("Stopping the monitoring stack…"):
        try:
            observability_off(purge=purge)
        except RuntimeError as exc:
            _fail(exc)
    _console.print(
        "🛑 [bold]Observability off.[/]"
        + (" History deleted." if purge else " History kept for the next `on`.")
    )


@observability_app.command("status")
def observability_status() -> None:
    """Show the monitoring stack, its settings, and live run links."""
    from nika.metrics.share import run_links
    from nika.metrics.stack import status

    _console.print(_status_panel(status()))
    live = [link for link in run_links() if link["live"]]
    if live:
        _console.print()
        _print_links(live, title="🔗 Live run dashboards")


@observability_app.command("links")
def observability_links(
    result_dir: Path | None = typer.Option(
        None, "--result-dir", help="Show the link saved in this run's result directory."
    ),
) -> None:
    """Show the dashboard links of benchmark runs, live and expired."""
    from nika.metrics.share import is_live, read_dashboard, run_links

    if result_dir is not None:
        record = read_dashboard(result_dir)
        if record is None:
            _console.print(f"⚪ No dashboard link saved in {result_dir}.")
            raise typer.Exit(code=1)
        live = is_live(record)
        record = {**record, "live": live}
        _print_links([record], title="🔗 Run dashboard")
        return
    links = run_links()
    if not links:
        _console.print(
            "⚪ No run has a dashboard link yet. Enable "
            "[bold]nika.metrics.share[/] in config/nika.yaml."
        )
        return
    _print_links(links, title="🔗 Run dashboards")


@observability_app.command("share")
def observability_share(
    result_dir: Path = typer.Option(
        ..., "--result-dir", help="Result directory of the benchmark run to share."
    ),
) -> None:
    """Open a dashboard link for an existing benchmark run, such as a finished one."""
    from nika.metrics.share import share_result_dir
    from nika.metrics.stack import is_observability_on

    if not is_observability_on():
        _fail(RuntimeError("observability is off; run `nika observability on` first"))
    with _console.status("Opening a Cloudflare tunnel…"):
        try:
            record = share_result_dir(result_dir)
        except RuntimeError as exc:
            _fail(exc)
    _print_links([{**record, "live": True}], title="🔗 Run dashboard")


@observability_app.command("unshare")
def observability_unshare(
    run_id: str = typer.Argument(..., help="Benchmark run id."),
) -> None:
    """Revoke a run's dashboard link now, before it expires."""
    from nika.metrics.share import unshare

    if unshare(run_id):
        _console.print(f"🔒 Link of run [bold]{run_id}[/] revoked.")
    else:
        _console.print(f"⚪ Run [bold]{run_id}[/] has no live link.")
