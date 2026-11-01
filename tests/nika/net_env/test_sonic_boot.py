"""SONiC-VS boot readiness and image load helpers (no Docker)."""

from __future__ import annotations

import hashlib
import io
import subprocess
from contextlib import nullcontext

import pytest

from nika.net_env.utils.sonic import common
from nika.net_env.utils.sonic.common import (
    BOOT_SERVICES,
    _boot_state,
    _loaded_image_ref,
    ensure_sonic_image,
)

pytestmark = pytest.mark.unit


class FakeRuntime:
    def __init__(self, output: str) -> None:
        self.output = output

    def exec(self, node: str, command: str, timeout: float = 20) -> str:
        del node, command, timeout
        return self.output


def _status(start_sh: str, *services: tuple[str, str], start_extra: str = "") -> str:
    lines = []
    for name, state in (("start.sh", start_sh), *services):
        if name == "start.sh" and state == "EXITED":
            lines.append(f"{name:32} EXITED    Oct 31 06:00 PM{start_extra}")
        elif state == "RUNNING":
            lines.append(f"{name:32} RUNNING   pid 8, uptime 0:00:40")
        else:
            lines.append(f"{name:32} {state}")
    return "\n".join(lines)


def _running() -> tuple[tuple[str, str], ...]:
    return tuple((name, "RUNNING") for name in BOOT_SERVICES)


@pytest.mark.parametrize(
    ("start_sh", "last_state", "expected"),
    [
        ("EXITED", "RUNNING", "ready"),
        ("EXITED", None, "booting"),
        ("EXITED", "STARTING", "booting"),
        ("EXITED", "BACKOFF", "booting"),
        ("EXITED", "FATAL", "failed"),
        ("EXITED", "STOPPED", "failed"),
        ("EXITED", "EXITED", "failed"),
        ("RUNNING", "RUNNING", "booting"),
    ],
)
def test_boot_state_follows_start_script_and_service_status(
    start_sh: str, last_state: str | None, expected: str
) -> None:
    extra = () if last_state is None else ((BOOT_SERVICES[-1], last_state),)
    start_extra = "   (exit status 1)" if start_sh == "EXITED" else ""
    output = _status(start_sh, *_running()[:-1], *extra, start_extra=start_extra)
    assert _boot_state(FakeRuntime(output), "r1") == expected


def test_loaded_image_ref_reads_name_and_id_lines() -> None:
    assert _loaded_image_ref("Loaded image: docker-sonic-vs:latest\n") == (
        "docker-sonic-vs:latest"
    )
    assert _loaded_image_ref("Loaded image ID: sha256:abc\n") == "sha256:abc"
    assert _loaded_image_ref("nothing") is None


def test_ensure_sonic_image_skips_load_or_tags_without_rmi(
    monkeypatch, tmp_path
) -> None:
    payload = b"gz"
    monkeypatch.setattr("nika.config.CACHE_DIR", tmp_path)
    monkeypatch.setattr(common, "ARTIFACT_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(
        common.urllib.request,
        "urlopen",
        lambda *a, **k: nullcontext(io.BytesIO(payload)),
    )

    monkeypatch.setattr(common, "image_exists", lambda image: True)
    monkeypatch.setattr(
        common.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("docker must not run")),
    )
    ensure_sonic_image()

    seen = {"n": 0}

    def exists_after_lock(image: str) -> bool:
        seen["n"] += 1
        return seen["n"] > 1

    monkeypatch.setattr(common, "image_exists", exists_after_lock)
    ensure_sonic_image()
    assert seen["n"] == 2

    pinned = {"ready": False}
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["docker", "load"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="Loaded image ID: sha256:abc\n", stderr=""
            )
        if cmd[:2] == ["docker", "tag"]:
            pinned["ready"] = True
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(common, "image_exists", lambda image: pinned["ready"])
    monkeypatch.setattr(common.subprocess, "run", run)
    ensure_sonic_image("docker-sonic-vs:pinned")
    assert ["docker", "tag", "sha256:abc", "docker-sonic-vs:pinned"] in calls
    assert all(cmd[:2] != ["docker", "rmi"] for cmd in calls)
