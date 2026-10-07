"""nika config: show and update run configuration."""

from __future__ import annotations

import re

import typer
import yaml
from pydantic import BaseModel

from nika.run_config.loader import (
    ENV_RUN_CONFIG,
    load_run_config,
    resolve_run_config_path,
)
from nika.run_config.schema import RunConfig

config_app = typer.Typer(help="Run configuration (config/nika.yaml).")

_KEY_LINE_RE = re.compile(r"^(?P<indent> *)(?P<key>[A-Za-z0-9_-]+):(?P<rest>.*)$")


def _check_config_key(path: str) -> None:
    """Reject dotted keys that do not name a scalar field of ``RunConfig``."""
    model: type[BaseModel] = RunConfig
    parts = path.split(".")
    for index, part in enumerate(parts):
        field = model.model_fields.get(part)
        if field is None:
            raise typer.BadParameter(
                f"Unknown key {path!r} (no field {part!r}). See `nika config show`."
            )
        annotation = field.annotation
        is_model = isinstance(annotation, type) and issubclass(annotation, BaseModel)
        if index < len(parts) - 1:
            if not is_model:
                raise typer.BadParameter(f"Unknown key {path!r}.")
            model = annotation
        elif is_model:
            raise typer.BadParameter(
                f"Key {path!r} is a section; set one of its fields instead."
            )


def _set_yaml_line(text: str, path: str, raw_value: str) -> str:
    """Overwrite ``path`` in YAML ``text`` in place, keeping comments and layout.

    Replaces the value on the existing key line; otherwise inserts the key (and
    missing parent sections, 2-space indent) after its deepest existing parent.
    """
    parts = path.split(".")
    lines = text.splitlines()
    stack: list[tuple[int, str]] = []
    # depth -> index after the last line inside the block ``parts[:depth]``.
    block_end = {0: len(lines)}
    for lineno, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        while stack and stack[-1][0] >= indent:
            stack.pop()
        match = _KEY_LINE_RE.match(line)
        keys = [key for _, key in stack] + ([match.group("key")] if match else [])
        if keys == parts:
            lines[lineno] = f"{match.group('indent')}{keys[-1]}: {raw_value}"
            return "\n".join(lines) + "\n"
        for depth in range(1, min(len(keys), len(parts) - 1) + 1):
            if keys[:depth] == parts[:depth]:
                block_end[depth] = lineno + 1
        if match:
            stack.append((indent, match.group("key")))
    depth = max(block_end)
    new_lines = [f"{'  ' * d}{parts[d]}:" for d in range(depth, len(parts) - 1)]
    new_lines.append(f"{'  ' * (len(parts) - 1)}{parts[-1]}: {raw_value}")
    lines[block_end[depth] : block_end[depth]] = new_lines
    return "\n".join(lines) + "\n"


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
            "Dotted config key assignment(s), e.g. agent.model=gpt-5 or "
            "benchmark.batch_size=4. Any scalar key shown by `nika config show`."
        ),
    ),
    run_config: str | None = typer.Option(
        None,
        "--run-config",
        envvar=ENV_RUN_CONFIG,
        help="Path to config/nika.yaml (default: config/nika.yaml).",
    ),
) -> None:
    """Overwrite run-config keys in the YAML file in place (keeps comments)."""
    path = resolve_run_config_path(run_config)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""

    for raw in assignments:
        if "=" not in raw:
            raise typer.BadParameter(f"Invalid assignment {raw!r}. Use KEY=VALUE.")
        key, raw_value = (part.strip() for part in raw.split("=", 1))
        if not key:
            raise typer.BadParameter(
                f"Invalid assignment {raw!r}. Key cannot be empty."
            )
        _check_config_key(key)
        try:
            parsed = yaml.safe_load(raw_value)
        except yaml.YAMLError as exc:
            raise typer.BadParameter(f"Invalid value {raw_value!r}: {exc}") from exc
        if isinstance(parsed, (dict, list)):
            raise typer.BadParameter(
                f"Value for {key!r} must be a scalar; edit {path} for lists/maps."
            )
        text = _set_yaml_line(text, key, raw_value)

    try:
        loaded = yaml.safe_load(text) or {}
        RunConfig.model_validate(loaded)
    except Exception as exc:
        raise typer.BadParameter(f"Invalid configuration after set: {exc}") from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    typer.secho(f"Wrote {path}", fg=typer.colors.GREEN)
    for raw in assignments:
        typer.echo(f"  {raw.strip()}")
