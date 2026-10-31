"""Containerlab runtime re-joins wires on reuse and keys inspect/destroy by name."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nika.runtime.containerlab.runtime import ContainerlabRuntime
from nika.runtime.containerlab.wires import write_wired_topology

pytestmark = pytest.mark.unit

_TOPO = """\
name: demo
topology:
  nodes:
    a: {}
    b: {}
  links:
    - endpoints: [a:eth1, b:eth1]
"""


def _runtime(tmp_path: Path) -> tuple[ContainerlabRuntime, Path, list[tuple[str, str]]]:
    topo = tmp_path / "demo.clab.yml"
    topo.write_text(_TOPO, encoding="utf-8")
    _, wires = write_wired_topology(topo, "demo")
    runtime = ContainerlabRuntime(lab_name="demo", topology_file=topo)
    return runtime, topo, wires


def test_deploy_rejoins_wires_from_wired_topology_when_lab_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _topo, wires = _runtime(tmp_path)
    monkeypatch.setattr(runtime, "exists", lambda: True)
    monkeypatch.setattr(runtime, "_refresh_node_map", lambda: True)

    def boom(*args: str) -> subprocess.CompletedProcess[str]:
        raise AssertionError(args)

    monkeypatch.setattr(runtime, "_run_clab", boom)
    with patch("nika.runtime.containerlab.wires.join_wires") as join:
        assert runtime.deploy() is False
    join.assert_called_once_with(wires)


def test_inspect_and_destroy_key_by_lab_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime, _topo, wires = _runtime(tmp_path)
    calls: list[tuple[str, ...]] = []

    def fake_clab(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(["clab", *args], 0, stdout="{}", stderr="")

    monkeypatch.setattr(runtime, "_run_clab", fake_clab)
    monkeypatch.setattr(
        "nika.service.containerlab.host_tc.HostTcController.cleanup_lab",
        lambda lab: None,
    )
    monkeypatch.setattr(
        "nika.runtime.shared.containers.remove_lab_helpers",
        lambda lab: None,
    )
    monkeypatch.setattr(
        "nika.runtime.containerlab.wires.remove_leftover_wire_ends",
        lambda ends: list(ends),
    )

    assert runtime.inspect() == []
    runtime.destroy()

    assert calls[0][:3] == ("inspect", "--name", "demo")
    assert "-t" not in calls[0]
    assert calls[1][:3] == ("destroy", "--name", "demo")
    assert "-t" not in calls[1]
    out = capsys.readouterr().out
    assert "Removed leftover host wires for demo:" in out
    for end in (wires[0][0], wires[0][1]):
        assert end in out


def test_inspect_survives_when_clab_keys_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ContainerlabRuntime(lab_name="lab", topology_file="/tmp/topo.yml")
    container = MagicMock()
    runtime._node_containers = {"r1": container}
    failed = subprocess.CompletedProcess(["clab"], 1, stdout="", stderr="busy")
    captured: list[tuple[str, ...]] = []

    def fake_clab(*args: str) -> subprocess.CompletedProcess[str]:
        captured.append(args)
        return failed

    monkeypatch.setattr(runtime, "_run_clab", fake_clab)
    assert runtime._refresh_node_map() is False
    assert runtime._cached_container("r1") is container
    assert captured[0][:3] == ("inspect", "--name", "lab")
