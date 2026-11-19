from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nika.problems.rca import UnresolvedRootCauseError
from nika.workflows.benchmark.migrate import migrate_benchmark_yaml

pytestmark = pytest.mark.contract


def _write_yaml(path: Path, payload: dict) -> None:
    path.write_text(yaml.dump(payload, sort_keys=False), encoding="utf-8")


def test_materializes_repeated_fault_placements() -> None:
    from nika.problems.combinations import get_combination
    from nika.workflows.benchmark.migrate import materialize_case

    row = get_combination("redundant_links_both_lost").rows()[0]
    materialized = materialize_case(row)
    assert materialized["inject"] == row["inject"]
    causes = materialized["root_causes"]
    assert len(causes) == 2
    assert {cause["fault_type"] for cause in causes} == {"link_down"}
    assert len({cause["resource"]["name"] for cause in causes}) == 2


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
            "kind": "link",
            "name": "pc1:eth0--router1:eth1",
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
