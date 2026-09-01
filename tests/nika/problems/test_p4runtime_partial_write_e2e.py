"""Docker E2E for p4runtime_partial_write on p4_dc_fabric and p4_dc_gateway."""

from __future__ import annotations

import time

import pytest

from nika.workflows.benchmark.inject_resolve import resolve_inject_params
from nika.net_env.p4_dc_fabric.fabric_manager.apply import reconcile_fabric
from nika.net_env.p4_dc_fabric.topology_model import build_clos_fabric_model
from nika.net_env.p4_dc_gateway.apply import reconcile_gateway
from nika.net_env.p4_dc_gateway.topology_model import build_gateway_fabric_model
from nika.net_env.verify import http_ok
from nika.problems.registry import get_problem_class, list_avail_problem_names
from nika.problems.support.probe_paths import get_probe_path
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available
from tests.support.symptom import evaluate_symptom, get_symptom_contract

SEEDS = (0, 1, 2, 3, 4)
TOPO_SIZES = ("s", "m")
REPEATS = (0, 1, 2)
SCENARIOS = ("p4_dc_fabric", "p4_dc_gateway")


def test_failure_registers_with_path_http_contract() -> None:
    assert "p4runtime_partial_write" in list_avail_problem_names()
    contract = get_symptom_contract("p4runtime_partial_write")
    assert contract.symptom_class == "unreachable"
    assert contract.probe == "path_http"


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("topo_size", TOPO_SIZES)
@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("repeat", REPEATS)
@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class P4RuntimePartialWriteE2ETest(IntegrationTestCase):
    def test_inject_verify_symptom_unreachable(
        self, scenario: str, topo_size: str, seed: int, repeat: int
    ) -> None:
        path = get_probe_path(scenario, topo_size=topo_size)
        assert path is not None and path.http_url

        params = resolve_inject_params(
            "p4runtime_partial_write", scenario, topo_size, seed=seed
        )
        assert params["host_name"].startswith("leaf_")
        if scenario == "p4_dc_gateway":
            model = build_gateway_fabric_model(topo_size)  # type: ignore[arg-type]
            assert params["host_name"] == model.backend_pool[0].attached_switch
        else:
            assert params["host_name"] == "leaf_1"

        session_id = None
        try:
            session_id = self._start_env(scenario, ["-s", topo_size])
            self._assert_session_ready(session_id, scenario)

            cls = get_problem_class("p4runtime_partial_write")
            assert cls is not None
            instance = self._problem(cls, session_id=session_id)
            parsed = instance.parse_params(params)
            runtime = instance.runtime

            assert http_ok(runtime, path.src_host, path.http_url)

            instance.inject_fault(parsed)
            time.sleep(1)

            verify = instance.verify_fault(parsed)
            assert verify["verified"] is True, verify
            assert verify["details"]["artifact"]["verified"] is True, verify
            assert verify["details"]["symptom"]["verified"] is True, verify

            ok, symptom = evaluate_symptom(
                runtime,
                "p4runtime_partial_write",
                parsed,
                scenario=scenario,
                topo_size=topo_size,
            )
            assert ok is True, symptom
            assert symptom.get("after", {}).get("http_ok") is False, symptom

            if scenario == "p4_dc_fabric":
                reconcile_fabric(runtime, build_clos_fabric_model(topo_size))  # type: ignore[arg-type]
            else:
                reconcile_gateway(
                    runtime, build_gateway_fabric_model(topo_size)  # type: ignore[arg-type]
                )
            assert http_ok(runtime, path.src_host, path.http_url)
        finally:
            if session_id is not None:
                self._close_session(session_id)
