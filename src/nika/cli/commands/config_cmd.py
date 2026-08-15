"""nika config: show and migrate run configuration."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import typer
import yaml
from dotenv import dotenv_values

from nika.config import REPO_ROOT
from nika.run_config.legacy import (
    CREDENTIAL_ENV_KEYS,
    REMOVED_ENV_KEYS,
    detect_legacy_operational_env,
    detect_removed_env,
    legacy_env_to_partial_dict,
)
from nika.run_config.loader import (
    ENV_RUN_CONFIG,
    default_run_config_path,
    dump_run_config,
    load_run_config,
)
from nika.run_config.schema import RunConfig, default_run_config

config_app = typer.Typer(help="Run configuration (config/nika.yaml).")


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@config_app.command("show")
def config_show(
    run_config: str | None = typer.Option(
        None,
        "--run-config",
        envvar=ENV_RUN_CONFIG,
        help="Path to config/nika.yaml.",
    ),
) -> None:
    """Print the effective run configuration (no secrets)."""
    cfg = load_run_config(run_config)
    typer.echo(
        yaml.safe_dump(cfg.to_display_dict(), sort_keys=False, allow_unicode=True)
    )


@config_app.command("migrate")
def config_migrate(
    env_file: Path = typer.Option(
        REPO_ROOT / ".env",
        "--env-file",
        help="Source .env to migrate operational settings from.",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Destination YAML path (default: config/nika.yaml).",
    ),
    write_env: bool = typer.Option(
        False,
        "--write-env",
        help="Backup and rewrite .env to credentials-only after confirmation.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip interactive confirmation (still prints the plan).",
    ),
) -> None:
    """Migrate operational settings from .env into config/nika.yaml."""
    out_path = output or default_run_config_path()
    values = {
        k: v
        for k, v in dotenv_values(env_file).items()
        if v is not None and str(v).strip()
    }
    # Also consider process env for keys present there
    import os

    for key in list(values) + list(CREDENTIAL_ENV_KEYS) + list(REMOVED_ENV_KEYS):
        if key in os.environ and os.environ[key].strip():
            values.setdefault(key, os.environ[key].strip())

    legacy = detect_legacy_operational_env(values)
    removed = detect_removed_env(values)
    partial = legacy_env_to_partial_dict(values)

    base = default_run_config().model_dump(mode="python")
    if out_path.is_file():
        existing = yaml.safe_load(out_path.read_text(encoding="utf-8")) or {}
        if isinstance(existing, dict):
            base = _deep_merge(base, existing)
    merged = _deep_merge(base, partial)
    cfg = RunConfig.model_validate(merged)

    typer.echo("=== NIKA config migrate ===")
    typer.echo(f"Source env: {env_file}")
    typer.echo(f"Target YAML: {out_path}")
    if legacy:
        typer.echo("Operational keys to migrate:")
        for key in legacy:
            typer.echo(f"  - {key}")
    else:
        typer.secho(
            "No operational keys found in .env — nothing to migrate.",
            fg=typer.colors.YELLOW,
        )
        typer.echo(
            "For a new setup, prefer: cp config/nika.example.yaml config/nika.yaml"
        )
        typer.echo("Continuing writes built-in defaults (agent.models.* stay null).")
    if removed:
        typer.echo("Removed keys (will not migrate; safe to delete from .env):")
        for key in removed:
            typer.echo(f"  - {key}")

    missing_hints: list[str] = []
    if not (cfg.agent.type or "").strip():
        missing_hints.append("agent.type")
    if not (cfg.agent.provider or "").strip():
        missing_hints.append("agent.provider")
    if cfg.agent.provider == "custom" and not (cfg.agent.custom.base_url or "").strip():
        missing_hints.append("agent.custom.base_url")
    if missing_hints:
        typer.secho(
            "Please review / fill after migrate: " + ", ".join(missing_hints),
            fg=typer.colors.YELLOW,
        )

    typer.echo("\nProposed YAML:")
    typer.echo(
        yaml.safe_dump(cfg.to_display_dict(), sort_keys=False, allow_unicode=True)
    )

    if not yes:
        if not typer.confirm("Write this file?", default=False):
            typer.echo("Aborted.")
            raise typer.Exit(code=1)

    dump_run_config(cfg, out_path)
    typer.secho(f"Wrote {out_path}", fg=typer.colors.GREEN)

    cred_lines = [
        "# Credentials only — operational settings live in config/nika.yaml",
        "# Copy from .env.example. Uncomment the provider(s) you use.",
        "",
        "# OPENAI_API_KEY=",
        "# ANTHROPIC_API_KEY=",
        "# DEEPSEEK_API_KEY=",
        "# NIKA_CUSTOM_API_KEY=",
        "# LANGFUSE_SECRET_KEY=",
        "# LANGFUSE_PUBLIC_KEY=",
        "",
    ]
    # Keep credentials in .env. Provider and endpoint selection belongs in YAML.
    for key in CREDENTIAL_ENV_KEYS:
        if key in values:
            # replace commented placeholder
            cred_lines = [
                line if not line.startswith(f"# {key}=") else f"{key}={values[key]}"
                for line in cred_lines
            ]
            if not any(line.startswith(f"{key}=") for line in cred_lines):
                cred_lines.append(f"{key}={values[key]}")

    suggested = "\n".join(cred_lines).rstrip() + "\n"
    typer.echo("\nSuggested credentials-only .env:")
    # Redact values in display
    for line in suggested.splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, _ = line.partition("=")
            typer.echo(f"{k}=***REDACTED***")
        else:
            typer.echo(line)

    if write_env:
        if not yes:
            if not typer.confirm(
                f"Rewrite {env_file} to credentials-only?", default=False
            ):
                typer.echo("Skipped .env rewrite.")
                return
        backup = env_file.with_suffix(env_file.suffix + ".bak")
        shutil.copy2(env_file, backup)
        env_file.write_text(suggested, encoding="utf-8")
        typer.secho(
            f"Backed up to {backup} and wrote {env_file}", fg=typer.colors.GREEN
        )
    else:
        typer.echo(
            "\nRe-run with --write-env to backup and replace .env with credentials-only."
        )
