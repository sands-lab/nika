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
  - scenario: min3clos
    problem: healthy
    telemetry: [netflow, prometheus, syslog]
    inject: {}
  - scenario: iosxr_simple_bgp
    problem: healthy
    telemetry: [prometheus, syslog]
    inject: {}
  - scenario: routeros_simple_bgp
    problem: healthy
    telemetry: [netflow, prometheus, syslog]
    inject: {}
"""


def test_case_yaml_keeps_telemetry_on_sdn_p4_and_containerlab(tmp_path: Path) -> None:
    path = tmp_path / "cases.yaml"
    path.write_text(_CASES)
    rows = {row["scenario"]: row for row in load_benchmark_yaml(path)}
    assert rows["sdn_l3_clos"]["telemetry"] == ["netflow", "prometheus", "syslog"]
    assert rows["p4_dc_fabric"]["telemetry"] == ["prometheus", "syslog"]
    assert rows["p4_dc_gateway"]["telemetry"] == ["prometheus", "syslog"]
    assert rows["min3clos"]["telemetry"] == ["netflow", "prometheus", "syslog"]
    assert rows["iosxr_simple_bgp"]["telemetry"] == ["prometheus", "syslog"]
    assert rows["routeros_simple_bgp"]["telemetry"] == [
        "netflow",
        "prometheus",
        "syslog",
    ]


def test_run_config_ignores_nika_telemetry_section(tmp_path: Path) -> None:
    path = tmp_path / "nika.yaml"
    path.write_text("nika:\n  telemetry:\n    subnet: 172.26.0.0/16\n")
    load_run_config(path)
