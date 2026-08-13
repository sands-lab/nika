from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nika.problems.root_cause import UnresolvedRootCauseError
from nika.workflows.benchmark.migrate import migrate_benchmark_yaml


def _write_yaml(path: Path, payload: dict) -> None:
    path.write_text(yaml.dump(payload, sort_keys=False), encoding="utf-8")


class MigrateBenchmarkTest:
    def test_migrates_link_down(self, tmp_path: Path) -> None:
        src = tmp_path / "in.yaml"
        _write_yaml(
            src,
            {
                "seed": 42,
                "cases": [
                    {
                        "scenario": "simple_bgp",
                        "topo_size": None,
                        "problem": "link_down",
                        "inject": {"host_name": "pc1", "intf_name": "eth0"},
                    }
                ],
            },
        )
        out = tmp_path / "out.yaml"
        report = tmp_path / "report.yaml"
        result = migrate_benchmark_yaml(
            input_path=src, output_path=out, report_path=report
        )
        assert result["unresolved_count"] == 0
        causes = yaml.safe_load(out.read_text())["cases"][0]["root_causes"]
        assert causes[0]["fault_type"] == "link_down"
        assert causes[0]["resource"] == {
            "kind": "interface",
            "node": "pc1",
            "name": "eth0",
        }

    def test_unresolved_is_reported(self, tmp_path: Path) -> None:
        src = tmp_path / "in.yaml"
        _write_yaml(
            src,
            {
                "seed": 1,
                "cases": [
                    {
                        "scenario": "simple_bgp",
                        "problem": "not_a_fault",
                        "inject": {"host_name": "pc1"},
                    }
                ],
            },
        )
        out = tmp_path / "out.yaml"
        report = tmp_path / "report.yaml"
        with pytest.raises(UnresolvedRootCauseError):
            migrate_benchmark_yaml(input_path=src, output_path=out, report_path=report)
        migrate_benchmark_yaml(
            input_path=src,
            output_path=out,
            report_path=report,
            allow_unresolved=True,
        )
        dumped = yaml.safe_load(report.read_text())
        assert dumped["unresolved_count"] == 1
        assert dumped["unresolved"][0]["problem"] == "not_a_fault"
        cases = yaml.safe_load(out.read_text())["cases"]
        assert cases[0]["root_causes_status"] == "unresolved"
