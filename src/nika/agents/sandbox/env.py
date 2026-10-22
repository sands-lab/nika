"""Sandbox logging helpers."""

from __future__ import annotations

from nika.agents.sandbox.redact import redact_env_value


def format_env_for_log(env: dict[str, str]) -> dict[str, str]:
    return {k: redact_env_value(k, v) for k, v in env.items()}
