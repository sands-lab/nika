from __future__ import annotations

from nika.evaluator.scoring import score_rca_v2
from nika.problems.rca import (
    healthy_ground_truth,
    link_resource,
    node_resource,
)
from nika.problems.rca import RootCause
from nika.workflows.eval.session import generic_eval

_LINK = "pc1:eth0--router1:eth0"
_LINK_OTHER = "pc2:eth0--router2:eth0"


def _gt(*causes: RootCause) -> dict:
    return {
        "schema_version": 3,
        "is_anomaly": True,
        "root_causes": [c.model_dump(mode="json") for c in causes],
    }


def _sub(*causes: RootCause) -> dict:
    return {"root_causes": [c.model_dump(mode="json") for c in causes]}


class ScoringTest:
    def test_v2_resource_id_submit(self) -> None:
        truth = RootCause(resource=link_resource(_LINK), fault_type="link_down")
        scores = score_rca_v2(
            {
                "root_causes": [
                    {"resource_id": f"link/{_LINK}", "fault_type": "link_down"}
                ]
            },
            _gt(truth),
        )
        assert scores["rca_f1"] == 1.0
        cause = RootCause(resource=link_resource(_LINK), fault_type="link_down")
        scores = score_rca_v2(_sub(cause), _gt(cause))
        assert scores["rca_f1"] == 1.0

    def test_v2_extra_prediction(self) -> None:
        truth = RootCause(resource=link_resource(_LINK), fault_type="link_down")
        extra = RootCause(resource=node_resource("r1"), fault_type="host_missing_ip")
        scores = score_rca_v2(_sub(truth, extra), _gt(truth))
        assert scores["rca_recall"] == 1.0
        assert scores["rca_precision"] == 0.5
        assert scores["rca_f1"] == 0.6667

    def test_v2_missing_prediction(self) -> None:
        a = RootCause(resource=link_resource(_LINK), fault_type="link_down")
        b = RootCause(resource=node_resource("r1"), fault_type="host_missing_ip")
        scores = score_rca_v2(_sub(a), _gt(a, b))
        assert scores["rca_precision"] == 1.0
        assert scores["rca_recall"] == 0.5

    def test_resource_right_type_wrong(self) -> None:
        truth = RootCause(resource=link_resource(_LINK), fault_type="link_down")
        pred = RootCause(resource=link_resource(_LINK), fault_type="link_flap")
        scores = score_rca_v2(_sub(pred), _gt(truth))
        assert scores["rca_f1"] == 0.0
        assert scores["localization_f1"] == 1.0
        assert scores["fault_type_f1"] == 0.0

    def test_type_right_resource_wrong(self) -> None:
        truth = RootCause(resource=link_resource(_LINK), fault_type="link_down")
        pred = RootCause(resource=link_resource(_LINK_OTHER), fault_type="link_down")
        scores = score_rca_v2(_sub(pred), _gt(truth))
        assert scores["rca_f1"] == 0.0
        assert scores["localization_f1"] == 0.0
        assert scores["fault_type_f1"] == 1.0

    def test_healthy_empty_sets(self) -> None:
        gt = healthy_ground_truth().model_dump(mode="json")
        scores = score_rca_v2({"root_causes": []}, gt)
        assert scores["rca_f1"] == 1.0

    def test_generic_eval_uses_resource_pairs(self) -> None:
        cause = RootCause(resource=link_resource(_LINK), fault_type="link_down")
        gt = {
            "schema_version": 3,
            "is_anomaly": True,
            "root_causes": [cause.model_dump(mode="json", exclude_none=True)],
        }
        submission = {
            "is_anomaly": True,
            "root_causes": [cause.model_dump(mode="json", exclude_none=True)],
        }
        payload = generic_eval(gt, submission)
        assert payload["rca_f1"] == 1.0
        assert payload["localization_f1"] == 1.0
