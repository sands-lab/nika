"""SONiC-VS boot readiness and image load helpers (no Docker)."""

from __future__ import annotations

import hashlib
import subprocess

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
    rows = [("start.sh", start_sh), *services]
    lines = []
    for name, state in rows:
        if name == "start.sh" and state == "EXITED":
            lines.append(f"{name:32} EXITED    Oct 31 06:00 PM{start_extra}")
        elif state == "RUNNING":
            lines.append(f"{name:32} RUNNING   pid 8, uptime 0:00:40")
        else:
            lines.append(f"{name:32} {state}")
    return "\n".join(lines)


def _running_services() -> tuple[tuple[str, str], ...]:
    return tuple((name, "RUNNING") for name in BOOT_SERVICES)


def test_exited_start_script_with_running_services_is_ready() -> None:
    output = _status("EXITED", *_running_services(), start_extra="   (exit status 1)")
    assert _boot_state(FakeRuntime(output), "r1") == "ready"


def test_transient_boot_service_after_start_script_exits_is_booting() -> None:
    present = _running_services()[:-1]
    output = _status("EXITED", *present, start_extra="   (exit status 1)")
    assert _boot_state(FakeRuntime(output), "r1") == "booting"
    starting = (*present, (BOOT_SERVICES[-1], "STARTING"))
    assert _boot_state(FakeRuntime(_status("EXITED", *starting)), "r1") == "booting"
    backoff = (*present, (BOOT_SERVICES[-1], "BACKOFF"))
    assert _boot_state(FakeRuntime(_status("EXITED", *backoff)), "r1") == "booting"


def test_stopped_boot_service_after_start_script_exits_is_failed() -> None:
    present = _running_services()[:-1]
    for state in ("FATAL", "STOPPED", "EXITED"):
        output = _status("EXITED", *present, (BOOT_SERVICES[-1], state))
        assert _boot_state(FakeRuntime(output), "r1") == "failed"


def test_start_script_still_running_is_booting() -> None:
    output = _status("RUNNING", *_running_services())
    assert _boot_state(FakeRuntime(output), "r1") == "booting"


def test_loaded_image_ref_reads_name_and_id_lines() -> None:
    assert _loaded_image_ref("Loaded image: docker-sonic-vs:latest\n") == (
        "docker-sonic-vs:latest"
    )
    assert _loaded_image_ref("Loaded image ID: sha256:abc\n") == "sha256:abc"
    assert _loaded_image_ref("nothing") is None


class _FakeDownload:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self, size: int) -> bytes:
        del size
        data, self._payload = self._payload, b""
        return data

    def __enter__(self) -> _FakeDownload:
        return self

    def __exit__(self, *args) -> None:
        return None


def _stub_image_load(monkeypatch, tmp_path, *, exists, run, payload: bytes = b"gz"):
    monkeypatch.setattr(common, "image_exists", exists)
    monkeypatch.setattr("nika.config.CACHE_DIR", tmp_path)
    monkeypatch.setattr(common, "ARTIFACT_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(
        common.urllib.request, "urlopen", lambda *args, **kwargs: _FakeDownload(payload)
    )
    monkeypatch.setattr(common.subprocess, "run", run)


def test_ensure_sonic_image_returns_when_present(monkeypatch, tmp_path) -> None:
    def run(*args, **kwargs):
        raise AssertionError("docker must not run when the pin exists")

    _stub_image_load(monkeypatch, tmp_path, exists=lambda image: True, run=run)
    ensure_sonic_image()


def test_ensure_sonic_image_rechecks_after_lock(monkeypatch, tmp_path) -> None:
    seen = {"n": 0}

    def exists(image: str) -> bool:
        del image
        seen["n"] += 1
        return seen["n"] > 1

    def run(*args, **kwargs):
        raise AssertionError("docker must not run after a concurrent load")

    _stub_image_load(monkeypatch, tmp_path, exists=exists, run=run)
    ensure_sonic_image()
    assert seen["n"] == 2


def test_ensure_sonic_image_tags_loaded_id_and_keeps_source(
    monkeypatch, tmp_path
) -> None:
    pinned = {"ready": False}
    calls: list[list[str]] = []

    def exists(image: str) -> bool:
        return pinned["ready"]

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

    _stub_image_load(monkeypatch, tmp_path, exists=exists, run=run)
    ensure_sonic_image("docker-sonic-vs:pinned")
    assert ["docker", "tag", "sha256:abc", "docker-sonic-vs:pinned"] in calls
    assert all(cmd[:2] != ["docker", "rmi"] for cmd in calls)
