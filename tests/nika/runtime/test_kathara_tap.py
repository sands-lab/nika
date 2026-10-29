"""TAP ownership aliases, including sessions recorded before the neutral prefix."""

from __future__ import annotations

import json
import shlex
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nika.runtime.base import RuntimeCapabilityError
from nika.runtime.kathara import tap

pytestmark = pytest.mark.unit

LAB = "isp_abilene__lab1"


class _Container:
    def __init__(self, listing: str, *, exit_code: int = 0, status: str = "running"):
        self.name = "router1"
        self.id = "c1"
        self.status = status
        self.listing = listing
        self.exit_code = exit_code
        self.commands: list[list[str]] = []

    def exec_run(self, command):
        self.commands.append(command)
        return SimpleNamespace(exit_code=self.exit_code, output=self.listing.encode())


def _script(container: _Container) -> str:
    assert container.commands
    assert container.commands[0][:2] == ["/bin/sh", "-c"]
    return container.commands[0][2]


def test_records_all_taps_in_one_exec() -> None:
    container = _Container("")
    tap.record_container_taps(container, LAB)
    owner = tap.tap_owner(LAB)
    script = _script(container)
    assert len(container.commands) == 1
    assert owner in script
    assert f"legacy={shlex.quote('nika:' + owner)}" in script
    assert "NEED_HELPER" in script
    assert "FOREIGN" in script


def test_foreign_alias_is_still_rejected() -> None:
    other = tap.tap_owner("other_lab")
    container = _Container(f"FOREIGN\teth0\t{other}\n", exit_code=1)
    with pytest.raises(RuntimeCapabilityError, match="already has alias"):
        tap.record_container_taps(container, LAB)


def test_inspect_failure_without_protocol_lines_is_raised() -> None:
    container = _Container("permission denied\n", exit_code=1)
    with pytest.raises(RuntimeCapabilityError, match="Cannot inspect TAPs"):
        tap.record_container_taps(container, LAB)


def test_helper_runs_once_for_all_failed_taps() -> None:
    container = _Container("NEED_HELPER\teth0\nNEED_HELPER\teth1\n")
    with patch.object(tap, "_run_net_admin_helper") as helper:
        tap.record_container_taps(container, LAB)
    helper.assert_called_once()
    command, network_mode, lab_name, _error = helper.call_args.args
    assert lab_name == LAB
    assert network_mode == "container:c1"
    assert command[:2] == ["/bin/sh", "-ec"]
    assert "eth0" in command[2] and "eth1" in command[2]
    assert tap.tap_owner(LAB) in command[2]


def test_record_lab_taps_visits_every_running_container() -> None:
    running = [_Container("", status="running") for _ in range(3)]
    for index, container in enumerate(running):
        container.name = f"r{index}"
        container.id = f"c{index}"
    stopped = _Container("", status="exited")
    tap.record_lab_taps([*running, stopped], LAB)
    assert all(len(container.commands) == 1 for container in running)
    assert stopped.commands == []


def test_record_lab_taps_can_swallow_per_container_errors() -> None:
    bad = _Container("FOREIGN\teth0\tother\n", exit_code=1)
    seen: list[str] = []
    tap.record_lab_taps([bad], LAB, on_error=lambda _c, exc: seen.append(str(exc)))
    assert seen and "already has alias" in seen[0]


def test_host_cleanup_finds_current_and_legacy_aliases() -> None:
    owner = tap.tap_owner(LAB)
    links = [
        {"ifname": "tapa", "ifalias": owner},
        {"ifname": "tapb", "ifalias": "nika:" + owner},
        {"ifname": "tapc", "ifalias": tap.tap_owner("other_lab")},
    ]
    for link in links:
        link["linkinfo"] = {"info_data": {"type": "tap"}}
    result = SimpleNamespace(stdout=json.dumps(links))
    with patch.object(tap.subprocess, "run", return_value=result):
        found = tap.owned_host_taps(LAB)
    assert [link["ifname"] for link in found] == ["tapa", "tapb"]
