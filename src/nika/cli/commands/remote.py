"""Commands for the optional NIKA Remote lab-host daemon."""

from __future__ import annotations

import typer

from nika.remote.config import ENV_REMOTE_TOKEN, ENV_REMOTE_URL

remote_app = typer.Typer(help="NIKA Remote lab-host control plane.")


@remote_app.command("serve")
def remote_serve(
    host: str = typer.Option(
        "0.0.0.0",
        "--host",
        help="Bind address for the remote daemon.",
    ),
    port: int = typer.Option(
        8700,
        "--port",
        "-p",
        help="TCP port for the remote daemon.",
    ),
    token: str | None = typer.Option(
        None,
        "--token",
        envvar=ENV_REMOTE_TOKEN,
        help="Optional bearer token (also NIKA_REMOTE_TOKEN).",
    ),
) -> None:
    """Run the lab-host remote daemon (env/failure/session/MCP)."""
    from nika.remote.server import serve_remote

    typer.echo(f"Starting NIKA Remote on http://{host}:{port}")
    serve_remote(host=host, port=port, token=token)


@remote_app.command("health")
def remote_health(
    url: str | None = typer.Option(
        None,
        "--url",
        envvar=ENV_REMOTE_URL,
        help="Remote daemon base URL (default: NIKA_REMOTE_URL).",
    ),
) -> None:
    """Probe a remote daemon ``/health`` endpoint."""
    import os

    from nika.remote.client import RemoteClient, RemoteError
    from nika.remote.config import RemoteConfig, load_remote_config

    if url:
        cfg = RemoteConfig(
            enabled=True,
            url=url,
            token=os.environ.get(ENV_REMOTE_TOKEN, "").strip(),
            artifact_root="",
        )
        client = RemoteClient(cfg)
    else:
        client = RemoteClient(load_remote_config())
    try:
        resp = client.health()
    except RemoteError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"{resp.status} ({resp.role}) @ {client.config.base_url}")
