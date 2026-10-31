"""SONiC-VS boot readiness from supervisorctl status (no Docker)."""

from __future__ import annotations

import pytest

from nika.net_env.utils.sonic.common import BOOT_SERVICES, _boot_state

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


def test_missing_boot_service_after_start_script_exits_is_failed() -> None:
    present = _running_services()[:-1]
    output = _status("EXITED", *present, start_extra="   (exit status 1)")
    assert _boot_state(FakeRuntime(output), "r1") == "failed"


def test_start_script_still_running_is_booting() -> None:
    output = _status("RUNNING", *_running_services())
    assert _boot_state(FakeRuntime(output), "r1") == "booting"
