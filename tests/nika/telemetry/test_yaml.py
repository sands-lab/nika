"""Benchmark YAML selects telemetry kinds on a case row."""

from __future__ import annotations

from pathlib import Path

import pytest

from nika.run_config.loader import load_run_config
from nika.workflows.benchmark.load_config import load_benchmark_yaml

pytestmark = pytest.mark.contract

_CASES = """\
cases:
  - scenario: sdn_l3_clos
    problem: healthy
    topo_size: s
    telemetry: [netflow, prometheus, syslog]
    inject: {}
  - scenario: p4_dc_fabric
    problem: healthy
    topo_size: s
    telemetry: [prometheus, syslog]
    inject: {}
  - scenario: p4_dc_gateway
    problem: healthy
    topo_size: s
    telemetry: [prometheus, syslog]
    inject: {}
  - scenario: dc_clos
    backend: containerlab
    device_profile: nokia_srlinux
    problem: healthy
    telemetry: [netflow, prometheus, syslog]
    inject: {}
  - scenario: dc_clos
    device_profile: iosxr
    problem: healthy
    telemetry: [prometheus, syslog]
    inject: {}
  - scenario: dc_clos
    device_profile: routeros
    problem: healthy
    telemetry: [netflow, prometheus, syslog]
    inject: {}
"""


def test_case_yaml_keeps_telemetry_on_sdn_p4_and_containerlab(tmp_path: Path) -> None:
    path = tmp_path / "cases.yaml"
    path.write_text(_CASES)
    rows = {
        (row["scenario"], row.get("device_profile")): row
        for row in load_benchmark_yaml(path)
    }
    assert rows[("sdn_l3_clos", None)]["telemetry"] == [
        "netflow",
        "prometheus",
        "syslog",
    ]
    assert rows[("p4_dc_fabric", None)]["telemetry"] == ["prometheus", "syslog"]
    assert rows[("p4_dc_gateway", None)]["telemetry"] == ["prometheus", "syslog"]
    assert rows[("dc_clos", "nokia_srlinux")]["telemetry"] == [
        "netflow",
        "prometheus",
        "syslog",
    ]
    assert rows[("dc_clos", "iosxr")]["telemetry"] == ["prometheus", "syslog"]
    assert rows[("dc_clos", "routeros")]["telemetry"] == [
        "netflow",
        "prometheus",
        "syslog",
    ]


def test_run_config_ignores_nika_telemetry_section(tmp_path: Path) -> None:
    path = tmp_path / "nika.yaml"
    path.write_text("nika:\n  telemetry:\n    subnet: 172.26.0.0/16\n")
    load_run_config(path)
