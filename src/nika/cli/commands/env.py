"""Commands for deploying and listing network environments."""

import typer

from nika.cli.utils import env_id_from_lab, fmt_table, human_age
from nika.config import ENV_RESULT_DIR

env_app = typer.Typer(help="Network lab scenarios (Kathara and Containerlab).")


@env_app.command("list")
def env_list() -> None:
    """Print scenario ids registered in the net env pool."""
    from nika.net_env.net_env_pool import list_all_net_envs

    headers = ["SCENARIO", "BACKENDS"]
    rows = []
    for name in sorted(list_all_net_envs()):
        cls = list_all_net_envs()[name]
        backends = ", ".join(cls.SUPPORTED_BACKENDS)
        rows.append([name, backends])
    typer.echo(fmt_table(headers, rows))


@env_app.command("run")
def env_run(
    name: str = typer.Argument(
        ..., metavar="NAME", help="Scenario id (see `nika env list`)."
    ),
    size: str | None = typer.Option(
        None,
        "-s",
        "--size",
        help="Topology size s, m, or l (required only for scalable scenarios).",
    ),
    igp: str | None = typer.Option(
        None,
        "--igp",
        help="Interior gateway protocol: isis or ospf (isp only; default: isis).",
    ),
    metric_strategy: str | None = typer.Option(
        None,
        "--metric-strategy",
        help=(
            "Link metric strategy: constant, routing_cost, or inv_capacity "
            "(isp only; default: constant)."
        ),
    ),
    constant_metric: int | None = typer.Option(
        None,
        "--constant-metric",
        help="Metric value for constant strategy / fallbacks (isp only; default: 10).",
    ),
    bgp_mode: str | None = typer.Option(
        None,
        "--bgp-mode",
        help=("BGP preset: none, ibgp_rr, or ebgp (isp only; default: none)."),
    ),
    rpki: bool = typer.Option(
        False,
        "--rpki/--no-rpki",
        help=(
            "Enable offline RPKI/ROV on eBGP (isp + Kathara only; requires "
            "--bgp-mode ebgp)."
        ),
    ),
    backend: str | None = typer.Option(
        None,
        "--backend",
        help=(
            "Lab backend: kathara or containerlab "
            "(required when a scenario supports both; isp defaults to kathara)."
        ),
    ),
    device_profile: str | None = typer.Option(
        None,
        "--device-profile",
        help=(
            "ISP device profile: frr (kathara) or nokia_srlinux (containerlab). "
            "Defaults from --backend."
        ),
    ),
    no_redeploy: bool = typer.Option(
        False,
        "--no-redeploy",
        help="If set, do not redeploy when the lab already exists.",
    ),
    static_validation: bool | None = typer.Option(
        None,
        "--static-validation/--no-static-validation",
        help=(
            "Run the optional Batfish verifier before deployment (ISP Kathara FRR only). "
            "Defaults to nika.static_validation.enabled."
        ),
    ),
    instance_tag: str | None = typer.Option(
        None,
        "--instance-tag",
        help="Optional tag for lab instance naming; required for human-friendly concurrent runs.",
    ),
    result_dir: str | None = typer.Option(
        None,
        "--result_dir",
        envvar=ENV_RESULT_DIR,
        help="Results parent directory (default: results/). Session output goes to {result_dir}/{session_id}.",
    ),
) -> None:
    """Deploy one scenario and start a new session."""
    from nika.workflows.env.start import start_net_env

    session_id = start_net_env(
        name,
        size,
        redeploy=not no_redeploy,
        instance_tag=instance_tag,
        result_dir=result_dir,
        igp=igp,
        metric_strategy=metric_strategy,
        constant_metric=constant_metric,
        bgp_mode=bgp_mode,
        rpki=rpki or None,
        backend=backend,
        device_profile=device_profile,
        static_validation=static_validation,
    )
    typer.echo(f"session_id={session_id}")


@env_app.command("ps")
def env_ps() -> None:
    """List running env instances, one row per deployed lab.

    Sessions are grouped by their lab instance so you can see at a glance
    how many sessions are active for each environment and how long it has
    been running.

    \b
    Columns
    -------
    ENV ID      scenario name plus instance suffix (e.g. dc_clos_a1b2c3)
    BACKEND     kathara or containerlab
    SIZE        topology size when applicable (s, m, l), — otherwise
    STATUS      running | finished
    AGE         time elapsed since the env was created
    SESSIONS    number of active sessions bound to this env
    ENDPOINT    service endpoint when available, — otherwise
    """
    from nika.runtime.factory import resolve_backend
    from nika.utils.session_store import SessionStore

    sessions = SessionStore().list_running_sessions()
    if not sessions:
        typer.echo("No running env instances.")
        return

    # Deduplicate by lab_name — one env row per distinct deployed lab.
    seen_labs: set[str] = set()
    headers = ["ENV ID", "BACKEND", "SIZE", "STATUS", "AGE", "SESSIONS", "ENDPOINT"]
    rows: list[list[str]] = []

    for item in sessions:
        lab_name: str = item.get("lab_name") or ""
        if lab_name in seen_labs:
            continue
        seen_labs.add(lab_name)

        env_id = env_id_from_lab(lab_name)
        backend = resolve_backend(item)
        size = item.get("scenario_topo_size") or "—"

        status = item.get("status", "—")
        age = human_age(item.get("created_at"))

        # Count all running sessions sharing this lab instance.
        active = sum(
            1
            for s in sessions
            if s.get("lab_name") == lab_name and s.get("status") == "running"
        )
        sessions_col = f"{active} active"

        endpoint = item.get("endpoint", "—")

        rows.append([env_id, backend, size, status, age, sessions_col, endpoint])

    typer.echo(fmt_table(headers, rows))
