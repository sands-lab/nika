"""Thin wrapper around the ``incus`` CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any


class IncusError(RuntimeError):
    """An ``incus`` command failed."""


def require_incus() -> None:
    if shutil.which("incus") is None:
        raise IncusError(
            "nika.snapshot needs the incus CLI; see docs/operations/vm-snapshots.md."
        )


def incus(
    *args: str,
    project: str | None = None,
    check: bool = True,
    stdin: bytes | None = None,
    timeout: float = 1800,
) -> str:
    """Run ``incus [--project P] ARGS`` and return stdout."""
    # Global flags precede the subcommand; after ``exec ... --`` they would be
    # passed to the guest command instead.
    cmd = ["incus", *(["--project", project] if project else []), *args]
    # Never inherit stdin: some commands (e.g. ``project create``) read YAML from
    # a non-terminal stdin until it closes.
    proc = subprocess.run(
        cmd,
        input=stdin if stdin is not None else b"",
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if check and proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()
        if "permissions to talk to the incus daemon" in detail:
            detail += " (add your user to the incus-admin group and log in again)"
        raise IncusError(f"{' '.join(cmd)} failed: {detail}")
    return proc.stdout.decode(errors="replace")


def list_instances() -> list[dict[str, Any]]:
    """Instances of the default project."""
    return json.loads(incus("list", "--format", "json") or "[]")


def list_projects() -> list[dict[str, Any]]:
    return json.loads(incus("project", "list", "--format", "json") or "[]")


def instance_exists(name: str) -> bool:
    return bool(
        incus("list", f"^{name}$", "--format", "csv", "-c", "n", check=False).strip()
    )


def snapshot_names(name: str) -> list[str]:
    out = incus("snapshot", "list", name, "--format", "csv", "-c", "n", check=False)
    return [line.strip() for line in out.splitlines() if line.strip()]


def config_get(name: str, key: str, project: str | None = None) -> str:
    return incus("config", "get", name, key, project=project).strip()


def guest(
    vm: str,
    script: str,
    *,
    stdin: bytes | None = None,
    timeout: float = 1800,
) -> str:
    """Run a shell script as root in the guest."""
    return incus("exec", vm, "--", "sh", "-c", script, stdin=stdin, timeout=timeout)
