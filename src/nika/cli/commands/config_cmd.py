"""nika config: show and update run configuration."""

from __future__ import annotations

from typing import Any

import typer
import yaml

from nika.run_config.loader import (
    ENV_RUN_CONFIG,
    load_run_config,
    resolve_run_config_path,
)
from nika.run_config.schema import RunConfig, default_run_config

config_app = typer.Typer(help="Run configuration (config/nika.yaml).")

# Important keys writable via `nika config set` (lab/mcp/k8s stay YAML-only).
CONFIG_SET_KEYS = frozenset(
    {
        "agent.type",
        "agent.provider",
        "agent.model",
        "agent.max_steps",
        "agent.timeout_sec",
        "agent.reasoning_effort",
        "agent.custom.base_url",
        "agent.enable_skills",
        "nika.result_dir",
        "nika.judge.provider",
        "nika.judge.model",
    }
)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _set_dotted(data: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cursor: dict[str, Any] = data
    for part in parts[:-1]:
        nested = cursor.get(part)
        if not isinstance(nested, dict):
            nested = {}
            cursor[part] = nested
        cursor = nested
    cursor[parts[-1]] = value


def _parse_config_value(raw: str) -> Any:
    """Parse a CLI value as a YAML scalar (bool/int/null/str)."""
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise typer.BadParameter(f"Invalid value {raw!r}: {exc}") from exc
    return parsed


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
        yaml.safe_dump(
            cfg.model_dump(mode="python"), sort_keys=False, allow_unicode=True
        )
    )


@config_app.command("set")
def config_set(
    assignments: list[str] = typer.Argument(
        ...,
        metavar="KEY=VALUE",
        help=(
            "Dotted config key assignment(s). Allowed keys: "
            + ", ".join(sorted(CONFIG_SET_KEYS))
        ),
    ),
    run_config: str | None = typer.Option(
        None,
        "--run-config",
        envvar=ENV_RUN_CONFIG,
        help="Path to config/nika.yaml (default: config/nika.yaml).",
    ),
) -> None:
    """Write important run-config keys into YAML (persists for later runs)."""
    if not assignments:
        raise typer.BadParameter("Provide at least one KEY=VALUE assignment.")

    path = resolve_run_config_path(run_config)
    existing: dict[str, Any] = {}
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise typer.BadParameter(f"Run config must be a mapping: {path}")
        existing = loaded

    for raw in assignments:
        if "=" not in raw:
            raise typer.BadParameter(f"Invalid assignment {raw!r}. Use KEY=VALUE.")
        key, raw_value = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise typer.BadParameter(
                f"Invalid assignment {raw!r}. Key cannot be empty."
            )
        if key not in CONFIG_SET_KEYS:
            raise typer.BadParameter(
                f"Unsupported key {key!r}. Allowed: {', '.join(sorted(CONFIG_SET_KEYS))}"
            )
        _set_dotted(existing, key, _parse_config_value(raw_value))

    merged = _deep_merge(default_run_config().model_dump(mode="python"), existing)
    try:
        RunConfig.model_validate(merged)
    except Exception as exc:
        raise typer.BadParameter(f"Invalid configuration after set: {exc}") from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            existing, default_flow_style=False, sort_keys=False, allow_unicode=True
        ),
        encoding="utf-8",
    )
    typer.secho(f"Wrote {path}", fg=typer.colors.GREEN)
    for raw in assignments:
        typer.echo(f"  {raw.strip()}")
