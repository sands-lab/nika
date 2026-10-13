"""Oracle CI: Problem -> ground truth -> OracleSolver submission -> evaluator == 1.0."""

from __future__ import annotations

from functools import cache
from typing import Any

import pytest

from nika.evaluator.score_status import SCORE_KEYS
from nika.mcp.servers.common.task_server import validate_root_cause_choices
from nika.problems.rca import RootCause, canonical_root_causes
from nika.problems.rca.inventory import catalog_resources, load_offline_net_env
from nika.workflows.agent.submission import fault_candidates
from nika.workflows.benchmark.migrate import materialize_case
from nika.workflows.benchmark.release import load_release
from nika.workflows.eval.session import build_eval_metrics_payload

pytestmark = pytest.mark.contract

_RELEASE = "0.2.0"
_ENV_KEYS = ("topo", "igp", "bgp_mode", "rpki", "backend", "device_profile")
_ENV_CACHE: dict[tuple[str, ...], Any] = {}


class OracleSolver:
    """Agent that reads ground truth and submits it in ``submit()`` format."""

    def solve(self, gt: dict) -> dict:
        pairs = {
            (RootCause.model_validate(item).resource_id, item["fault_type"])
            for item in gt["root_causes"]
        }
        return {
            "is_anomaly": gt["is_anomaly"],
            "root_causes": [
                {"resource_id": resource_id, "fault_type": fault_type}
                for resource_id, fault_type in sorted(pairs)
            ],
        }


@cache
def _catalog_ids(scenario: str, topo_size: str, env_items: tuple) -> frozenset[str]:
    env = load_offline_net_env(scenario, topo_size, **dict(env_items))
    return frozenset(item.id for item in catalog_resources(env))


def _cases() -> list[Any]:
    params = []
    for split in ("dev", "test"):
        for index, row in enumerate(load_release(_RELEASE, split=split).cases):
            case_id = f"{split}-{index:03d}-{row['scenario']}-{row['problem']}"
            params.append(pytest.param(row, id=case_id))
    return params


@pytest.mark.parametrize("row", _cases())
def test_oracle_submission_scores_one(row: dict) -> None:
    root_causes = materialize_case(row, env_cache=_ENV_CACHE)["root_causes"]
    assert root_causes == canonical_root_causes(row.get("root_causes") or [])
    gt = {"is_anomaly": bool(root_causes), "root_causes": root_causes}

    submission = OracleSolver().solve(gt)

    causes = submission["root_causes"]
    assert bool(causes) == submission["is_anomaly"]
    if causes:
        env_items = tuple(
            (key, row[key]) for key in _ENV_KEYS if row.get(key) not in (None, "", "-")
        )
        catalog = _catalog_ids(
            row["scenario"], str(row.get("topo_size") or ""), env_items
        )
        # k8s Services/NetworkPolicies enter the catalog only from a live cluster.
        catalog |= {
            c["resource_id"] for c in causes if c["resource_id"].startswith("k8s/")
        }
        _, errors = validate_root_cause_choices(
            causes, catalog_ids=set(catalog), fault_types=set(fault_candidates())
        )
        assert not errors, errors

    payload, status = build_eval_metrics_payload(
        gt=gt, submission=submission, trace_metrics={}
    )
    assert status == "scored"
    assert {key: payload[key] for key in SCORE_KEYS} == dict.fromkeys(SCORE_KEYS, 1.0)
