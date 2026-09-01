"""Commands for inspecting and managing active sessions."""

import json

import typer

from nika.cli.utils import env_id_from_lab, fmt_table

session_app = typer.Typer(help="Active session management.")


def _failure_summary(session: dict) -> str:
    """Summarise injected failures: name when there is one, count otherwise."""
    n_failures = session.get(
        "failure_count", len(session.get("failure_injections", []))
    )
    if n_failures == 0:
        return "0"
    if n_failures == 1:
        problem_names = session.get("problem_names")
        if isinstance(problem_names, list) and problem_names:
            return str(problem_names[0])
        injections = session.get("failure_injections", [])
        if len(injections) == 1:
            return str(injections[0].get("problem_name", "—"))
        return "1"
    problem_names = session.get("problem_names")
    if isinstance(problem_names, list) and len(problem_names) > 1:
        return "+".join(str(name) for name in problem_names)
    return str(n_failures)


def _agent_summary(session: dict) -> str:
    """Summarise agent activity stored in a session document."""
    agent_type = session.get("agent_type")
    if not agent_type:
        return "—"
    start = session.get("start_time")
    end = session.get("end_time")
    if start and not end:
        return f"1 running ({agent_type})"
    if start and end:
        return f"1 done ({agent_type})"
    return "—"


def _container_rows(containers: list[dict]) -> list[list[str]]:
    rows: list[list[str]] = []
    for item in containers:
        image = str(item.get("image", "—"))
        if len(image) > 40:
            image = image[:37] + "..."
        rows.append(
            [
                str(item.get("container_id", "—")),
                str(item.get("name", "—")),
                image,
                str(item.get("status", "—")),
                str(item.get("container_name", "—")),
            ]
        )
    return rows


def _echo_containers_table(containers: list[dict], *, prefix: str = "") -> None:
    if not containers:
        typer.echo(f"{prefix}containers  (none running)")
        return
    hdr = ["CONTAINER ID", "NAME", "IMAGE", "STATUS", "NAMES"]
    label = f"containers  ({len(containers)} running)"
    typer.echo(f"{prefix}{label}:")
    for line in fmt_table(hdr, _container_rows(containers)).splitlines():
        typer.echo(f"{prefix}  {line}")


@session_app.command("ps")
def session_ps(
    all_sessions: bool = typer.Option(
        False, "--all", "-a", help="Include finished sessions."
    ),
) -> None:
    """List sessions and their runtime status.

    By default only running sessions are shown. Pass --all to include
    finished ones.

    \b
    Columns
    -------
    SESSION ID  unique session identifier
    ENV ID      scenario name plus instance suffix (e.g. dc_clos_a1b2c3)
    STATUS      running | finished
    FAILURES    problem name when there is one, otherwise record count
    AGENTS      agent activity summary
    """
    from nika.workflows.session.list import list_sessions

    sessions = list_sessions(running_only=not all_sessions)

    if not sessions:
        typer.echo("No sessions found.")
        return

    headers = ["SESSION ID", "ENV ID", "STATUS", "FAILURES", "AGENTS"]
    rows: list[list[str]] = []
    for s in sessions:
        rows.append(
            [
                s.get("session_id", "—"),
                env_id_from_lab(s.get("lab_name")),
                s.get("status", "—"),
                _failure_summary(s),
                _agent_summary(s),
            ]
        )

    typer.echo(fmt_table(headers, rows))


@session_app.command("inspect")
def session_inspect(
    session_id: str | None = typer.Option(
        None,
        "--session_id",
        help="Target session id. Auto-selects when only one is running.",
    ),
    containers: bool = typer.Option(
        False,
        "--containers",
        "-c",
        help="Include a docker-ps-like table of lab containers.",
    ),
) -> None:
    """Show detailed information about a session.

    Prints the full session document as formatted JSON, with failure
    injection records summarised below the main body. Pass ``--containers``
    to also list running Kathara devices in the session lab.
    """
    from nika.workflows.session.containers import list_session_containers
    from nika.workflows.session.inspect import inspect_session

    try:
        data, injections = inspect_session(session_id)
    except (FileNotFoundError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(json.dumps(data, indent=2, default=str))

    if injections:
        typer.echo(
            f"\nfailure_injections  ({len(injections)} record{'s' if len(injections) != 1 else ''}):"
        )
        hdr = ["IDX", "PROBLEM", "STATUS", "PARAMS"]
        fi_rows: list[list[str]] = []
        for i, inj in enumerate(injections):
            params_raw = inj.get("injection_params", {})
            params_str = json.dumps(params_raw, default=str)
            if len(params_str) > 60:
                params_str = params_str[:57] + "..."
            fi_rows.append(
                [
                    str(i),
                    inj.get("problem_name", "—"),
                    inj.get("status", "—"),
                    params_str,
                ]
            )
        for line in fmt_table(hdr, fi_rows).splitlines():
            typer.echo("  " + line)
    else:
        typer.echo("\nfailure_injections  (none)")

    if containers:
        try:
            _, _, container_rows = list_session_containers(session_id)
        except (FileNotFoundError, ValueError) as exc:
            raise typer.BadParameter(str(exc)) from exc
        typer.echo("")
        _echo_containers_table(container_rows)


@session_app.command("containers")
def session_containers(
    session_id: str | None = typer.Option(
        None,
        "--session_id",
        help="Target session id. Auto-selects when only one is running.",
    ),
) -> None:
    """List Kathara containers running in the session lab (docker-ps style).

    \b
    Columns
    -------
    CONTAINER ID  short Docker container id
    NAME          device name inside the lab topology
    IMAGE         Kathara Docker image
    STATUS        container status (running, exited, …)
    NAMES         full Docker container name
    """
    from nika.workflows.session.containers import list_session_containers

    try:
        resolved_id, lab_name, container_rows = list_session_containers(session_id)
    except (FileNotFoundError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"session_id={resolved_id}  lab={lab_name}")
    if not container_rows:
        typer.echo("No containers running for this session.")
        return

    headers = ["CONTAINER ID", "NAME", "IMAGE", "STATUS", "NAMES"]
    typer.echo(fmt_table(headers, _container_rows(container_rows)))


@session_app.command("close")
def session_close(
    session_id: str | None = typer.Option(
        None,
        "--session_id",
        help="Target session id. Auto-selects when only one is running.",
    ),
    yes: bool = typer.Option(
        False, "-y", "--yes", help="Skip the confirmation prompt."
    ),
) -> None:
    """Close one session: stop containers and clean up runtime state.

    Pass ``--session_id`` to close a specific session. When omitted and
    only one session is running it is selected automatically.

    The Kathará lab is undeployed, all failure records are marked ended,
    the runtime workdir is removed when present, and the runtime session
    file is removed. Use ``nika session wipe`` to close every running session
    and remove leftover Kathara, Containerlab, and runtime resources.
    """
    from nika.workflows.session.close import close_session

    label = session_id if session_id else "the active session"
    if not yes:
        confirmed = typer.confirm(
            f"Stop lab containers and clear {label}?",
            default=False,
        )
        if not confirmed:
            raise typer.Abort()

    try:
        close_session(session_id=session_id)
    except (FileNotFoundError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"Closed: {label}")


@session_app.command("wipe")
def session_wipe(
    yes: bool = typer.Option(
        False, "-y", "--yes", help="Skip the confirmation prompt."
    ),
) -> None:
    """Close all running sessions and wipe leftover lab resources.

    Every running session is closed (lab undeployed, failure records ended,
    runtime workdir removed, runtime session file removed). Also runs
    ``kathara wipe``, ``clab destroy --all``, and removes leftover files
    under ``runtime/`` when session files are missing.
    """
    from nika.utils.session_store import SessionStore
    from nika.workflows.session.close import close_session

    running = SessionStore().list_running_sessions()
    leftover = "leftover Kathara and Containerlab resources"
    if running:
        label = f"all {len(running)} running session(s) and {leftover}"
    else:
        label = leftover
    if not yes:
        confirmed = typer.confirm(
            f"Stop lab containers and wipe {label}?", default=False
        )
        if not confirmed:
            raise typer.Abort()
    try:
        close_session(stop_all=True)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Wiped: {label}")
