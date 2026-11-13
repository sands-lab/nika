"""Published releases retain their task failures, ground truth, and ontology."""

from __future__ import annotations

import pytest

from nika.problems.compat import PUBLISHED_RELEASES
from nika.problems.compat.routing import published_fault_ontology
from nika.problems.rca.materialize import (
    assert_root_causes_match,
    ground_truth_for_case,
)
from nika.workflows.agent.submission import fault_candidates
from nika.workflows.benchmark.load_config import read_benchmark_document
from nika.workflows.benchmark.release import load_release

pytestmark = pytest.mark.contract


@pytest.mark.parametrize("version", sorted(PUBLISHED_RELEASES))
@pytest.mark.parametrize("split", ["dev", "test"])
def test_published_task_identity_and_ontology(version, split):
    release = load_release(version, split=split)
    raw_rows = read_benchmark_document(release.cases_path)["cases"]
    for raw, row in zip(raw_rows, release.cases, strict=True):
        assert row["problem"] == raw["problem"]
        if row["problem"] == "frr_service_down":
            truth = ground_truth_for_case(
                problem=row["problem"],
                params=row["inject"],
                scenario=row["scenario"],
                topo_size=row.get("topo_size") or "",
                release=version,
            )
            assert_root_causes_match(truth, row["root_causes"])
    ontology = {
        entry["id"]: entry["description"]
        for entry in published_fault_ontology(fault_candidates(version))
    }
    assert ontology["frr_service_down"] == "FRR routing daemon is unavailable."
    assert "routing_service_down" not in ontology


def test_current_routing_service_candidates():
    names = fault_candidates(None)
    assert "routing_service_down" in names
    assert "frr_service_down" not in names
