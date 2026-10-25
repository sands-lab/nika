"""TAP ownership aliases, including sessions recorded before the neutral prefix."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nika.runtime.base import RuntimeCapabilityError
from nika.runtime.kathara import tap

pytestmark = pytest.mark.unit

LAB = "isp_abilene__lab1"


class _Container:
    name = "router1"
    id = "c1"

    def __init__(self, listing: str):
        self.listing = listing
        self.commands: list[list[str]] = []

    def exec_run(self, command):
        self.commands.append(command)
        if command[0] == "/bin/sh":
            return SimpleNamespace(exit_code=0, output=self.listing.encode())
        return SimpleNamespace(exit_code=0, output=b"")


def test_legacy_alias_is_recognized_and_rewritten() -> None:
    container = _Container(f"eth0\tnika:{tap.tap_owner(LAB)}\n")
    tap.record_container_taps(container, LAB)
    assert container.commands[-1] == [
        "ip",
        "link",
        "set",
        "dev",
        "eth0",
        "alias",
        tap.tap_owner(LAB),
    ]


def test_foreign_alias_is_still_rejected() -> None:
    container = _Container(f"eth0\t{tap.tap_owner('other_lab')}\n")
    with pytest.raises(RuntimeCapabilityError, match="already has alias"):
        tap.record_container_taps(container, LAB)


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
