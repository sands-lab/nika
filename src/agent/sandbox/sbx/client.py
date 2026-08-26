"""Thin wrapper around the ``sbx`` CLI."""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from collections.abc import Sequence

logger = logging.getLogger(__name__)

SBX_BIN = "sbx"
_HUB_TOKEN_MARKERS = ("token is unverifiable", "jwks.json")
_HUB_RETRY_DELAYS_S = (2.0, 4.0)


def _sbx_env(env: dict[str, str] | None = None) -> dict[str, str]:
    from agent.sandbox.sbx.proxy import sbx_process_env

    return sbx_process_env(env)


def _is_hub_token_error(stderr: str) -> bool:
    text = stderr.lower()
    return any(marker in text for marker in _HUB_TOKEN_MARKERS)


def sbx_authenticated() -> bool:
    if not sbx_available():
        return False
    # ``sbx policy ls`` is slow on some hosts and may hang after the summary;
    # treat partial successful output as authenticated.
    try:
        proc = subprocess.run(
            [SBX_BIN, "policy", "ls"],
            env=_sbx_env(),
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
        )
        combined = f"{proc.stdout}\n{proc.stderr}"
        returncode = proc.returncode
    except subprocess.TimeoutExpired as exc:
        combined = f"{exc.stdout or ''}\n{exc.stderr or ''}"
        returncode = -1
    if "Not authenticated" in combined:
        return False
    if "has not been initialized" in combined:
        return True
    if "local-policy" in combined or "POLICY" in combined:
        return True
    return returncode == 0


def require_sbx_authenticated() -> None:
    """Fail before policy mutation when Docker Sandboxes has no login session."""
    if sbx_authenticated():
        return
    raise RuntimeError(
        "Docker Sandboxes is not authenticated. Run `sbx login` (or "
        "`sbx login --username <name> --password-stdin`) and retry. "
        "The NIKA sandbox upstream proxy cannot replace Docker authentication."
    )


def ensure_sbx_daemon() -> None:
    """Start the sbx daemon when it is not already running."""
    from agent.sandbox.sbx.proxy import sbx_daemon_running

    # ``sbx ls`` talks to Docker Hub; use daemon status so a Hub timeout
    # cannot look like a dead daemon and race a second ``daemon start``.
    if sbx_daemon_running():
        return

    env = _sbx_env()
    try:
        proc = subprocess.run(
            [SBX_BIN, "daemon", "start"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        combined = f"{proc.stdout}\n{proc.stderr}"
        if proc.returncode != 0 and "already running" not in combined.lower():
            raise RuntimeError(
                "Failed to start sbx daemon:\n"
                f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )
    except subprocess.TimeoutExpired as exc:
        combined = f"{exc.stdout or ''}\n{exc.stderr or ''}".lower()
        if "already running" in combined:
            return
        if sbx_daemon_running():
            return
        raise RuntimeError(
            "Timed out starting sbx daemon:\n"
            f"stdout:\n{exc.stdout or ''}\nstderr:\n{exc.stderr or ''}"
        ) from exc


def ensure_sbx_policy_initialized(preset: str = "balanced") -> None:
    """Initialize global network policy on first use."""
    proc = run_sbx_optional(["policy", "init", preset])
    combined = f"{proc.stdout}\n{proc.stderr}"
    if proc.returncode == 0:
        return
    if "already initialized" in combined:
        return
    raise RuntimeError(
        f"Failed to initialize sbx network policy ({preset}):\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def ensure_sbx_ready(*, policy_preset: str = "balanced") -> None:
    """Ensure daemon is up and network policy is initialized."""
    ensure_sbx_daemon()
    ensure_sbx_policy_initialized(policy_preset)


def sbx_available() -> bool:
    return shutil.which(SBX_BIN) is not None


def list_sbx_secret_services() -> set[str]:
    """Return service names present in ``sbx secret ls`` (e.g. openai, anthropic)."""
    proc = run_sbx_optional(["secret", "ls"])
    if proc.returncode != 0:
        raise RuntimeError(
            f"sbx secret ls failed (code {proc.returncode}):\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    services: set[str] = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        # SCOPE TYPE NAME SECRET...  e.g. (global) service openai ...
        scope, kind, name = parts[0], parts[1], parts[2]
        if kind != "service":
            continue
        if scope in {"SCOPE", "NAME"}:
            continue
        services.add(name)
    return services


def list_sbx_custom_secret_envs() -> set[str]:
    """Return env var names already configured via ``sbx secret set-custom``."""
    return set(list_sbx_custom_secrets())


def list_sbx_custom_secrets() -> dict[str, str]:
    """Return ``{env_var: placeholder}`` for ``sbx secret set-custom`` entries."""
    proc = run_sbx_optional(["secret", "ls"])
    if proc.returncode != 0:
        raise RuntimeError(
            f"sbx secret ls failed (code {proc.returncode}):\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    secrets: dict[str, str] = {}
    in_custom = False
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("CUSTOM SECRETS"):
            in_custom = True
            continue
        if not in_custom:
            continue
        parts = stripped.split()
        if len(parts) < 4:
            continue
        if parts[0] in {"SCOPE", "NAME"} or parts[1] == "TARGETS":
            continue
        # SCOPE TARGETS ENV PLACEHOLDER SECRET
        secrets[parts[2]] = parts[3]
    return secrets


def _redact_sbx_argv(args: Sequence[str]) -> str:
    """Hide secret values passed via ``--value`` / ``-t`` in error messages."""
    redacted: list[str] = []
    skip_next = False
    for part in args:
        if skip_next:
            redacted.append("***")
            skip_next = False
            continue
        if part in {"--value", "-t", "--token"}:
            redacted.append(part)
            skip_next = True
            continue
        redacted.append(part)
    return " ".join(redacted)


def run_sbx(
    args: Sequence[str],
    *,
    check: bool = True,
    capture_output: bool = False,
    text: bool = True,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    if not sbx_available():
        raise RuntimeError(
            "Docker Sandboxes CLI (sbx) is not available on PATH. "
            "Install docker-sbx and run `sbx login`."
        )
    cmd = [SBX_BIN, *args]
    logger.debug("Running sbx command: %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture_output,
        text=text,
        env=_sbx_env(env),
        input=input_text,
    )


def run_sbx_optional(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run sbx without raising on non-zero exit."""
    return run_sbx(args, check=False, capture_output=True, text=True)


def run_sbx_checked(
    args: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    proc: subprocess.CompletedProcess[str] | None = None
    for attempt, delay_s in enumerate((0.0, *_HUB_RETRY_DELAYS_S)):
        if delay_s:
            time.sleep(delay_s)
        proc = run_sbx(
            args,
            check=False,
            capture_output=True,
            text=True,
            env=env,
            input_text=input_text,
        )
        if proc.returncode == 0:
            return proc
        if attempt >= len(_HUB_RETRY_DELAYS_S) or not _is_hub_token_error(
            proc.stderr or ""
        ):
            break
    assert proc is not None
    raise RuntimeError(
        f"sbx {_redact_sbx_argv(args)} failed (code {proc.returncode}):\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def stream_sbx(
    args: Sequence[str],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    if not sbx_available():
        raise RuntimeError("Docker Sandboxes CLI (sbx) is not available on PATH.")
    return subprocess.Popen(
        [SBX_BIN, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=_sbx_env(env),
    )
