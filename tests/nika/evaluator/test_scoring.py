from __future__ import annotations

import pytest

from nika.evaluator.scoring import score_rca_v2
from nika.problems.rca import (
    healthy_ground_truth,
    interface_resource,
    link_resource,
    node_resource,
)
from nika.problems.rca import RootCause
from nika.workflows.eval.session import generic_eval

pytestmark = pytest.mark.unit

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

    @pytest.mark.parametrize(("is_anomaly", "expected"), [(False, 1.0), (True, 0.0)])
    def test_healthy_empty_sets_need_healthy_verdict(
        self, is_anomaly: bool, expected: float
    ) -> None:
        gt = healthy_ground_truth().model_dump(mode="json")
        scores = score_rca_v2({"is_anomaly": is_anomaly, "root_causes": []}, gt)
        assert scores["rca_f1"] == expected
        assert scores["localization_f1"] == expected
        assert scores["fault_type_f1"] == expected

    @pytest.mark.parametrize(
        ("truth", "pred"),
        [
            (link_resource(_LINK), interface_resource("router1", "eth0")),
            (node_resource("router1"), interface_resource("router1", "eth2")),
        ],
        ids=["link-endpoint", "node-interface"],
    )
    def test_interface_inside_gt_resource_matches(self, truth, pred) -> None:
        gt_cause = RootCause(resource=truth, fault_type="link_down")
        scores = score_rca_v2(
            _sub(RootCause(resource=pred, fault_type="link_down")), _gt(gt_cause)
        )
        assert scores["rca_f1"] == 1.0
        assert scores["localization_f1"] == 1.0

    def test_containment_is_one_to_one_and_directional(self) -> None:
        truth = RootCause(resource=link_resource(_LINK), fault_type="link_down")
        both_ends = _sub(
            RootCause(
                resource=interface_resource("pc1", "eth0"), fault_type="link_down"
            ),
            RootCause(
                resource=interface_resource("router1", "eth0"), fault_type="link_down"
            ),
        )
        scores = score_rca_v2(both_ends, _gt(truth))
        assert scores["rca_precision"] == 0.5 and scores["rca_recall"] == 1.0
        # Not an endpoint of the link.
        off_link = _sub(
            RootCause(
                resource=interface_resource("pc1", "eth1"), fault_type="link_down"
            )
        )
        assert score_rca_v2(off_link, _gt(truth))["localization_f1"] == 0.0
        # A coarser prediction does not match a finer ground truth.
        iface_truth = RootCause(
            resource=interface_resource("router1", "eth0"), fault_type="link_down"
        )
        coarse = _sub(
            RootCause(resource=node_resource("router1"), fault_type="link_down")
        )
        assert score_rca_v2(coarse, _gt(iface_truth))["localization_f1"] == 0.0

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
