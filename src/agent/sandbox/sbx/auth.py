"""Sandbox credential helpers (sentinel values only; never copy host auth files)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from agent.sandbox.config import ENV_SANDBOX_NAME

PROXY_MANAGED_SENTINEL = "proxy-managed"


def in_sandbox() -> bool:
    return (
        os.environ.get("NIKA_SANDBOX_EXECUTION", "").strip() == "1"
        or os.environ.get(ENV_SANDBOX_NAME, "").strip() != ""
    )


def apply_codex_auth(codex_home: Path) -> None:
    """Populate *codex_home/auth.json* for Codex without copying host secrets.

    Inside a sandbox, write a proxy-managed sentinel so the host credential
    proxy can inject the real value. Outside a sandbox, symlink the host
    login file when present, otherwise materialize an API-key auth.json from
    ``OPENAI_API_KEY`` (including a sentinel already in the environment).
    """
    codex_home.mkdir(parents=True, exist_ok=True)
    dest = codex_home / "auth.json"
    # A dangling symlink planted by the sandbox must not redirect the write.
    if dest.exists() or dest.is_symlink():
        return

    if in_sandbox():
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        # Official OpenAI API-key mode sets OPENAI_API_KEY=proxy-managed.
        # Subscription / OAuth leaves it unset so Codex uses the host proxy.
        if not api_key:
            return
        dest.write_text(
            json.dumps(
                {
                    "OPENAI_API_KEY": api_key,
                    "auth_mode": "apikey",
                }
            ),
            encoding="utf-8",
        )
        dest.chmod(0o600)
        return

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()

    global_auth = Path.home() / ".codex" / "auth.json"
    if global_auth.is_file():
        dest.symlink_to(global_auth)
        return

    if api_key:
        dest.write_text(
            json.dumps({"OPENAI_API_KEY": api_key, "auth_mode": "apikey"}),
            encoding="utf-8",
        )
        dest.chmod(0o600)
