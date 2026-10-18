from __future__ import annotations

import pytest
from typing import ClassVar
from nika.net_env.min3clos.verify import (
    CLIENT1,
    CLIENT2_IP,
    EXPECTED_NODES,
    verify_min3clos_lab,
)
from nika.runtime.factory import resolve_backend, runtime_for_session
from tests.support.integration_base import SharedSessionTestCase
from tests.support.prerequisites import containerlab_prerequisites
from tests.support.net_env import assert_verify_success

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    not containerlab_prerequisites(),
    reason="containerlab, gnmic, or Docker not available",
)
class Min3ClosVerifyIntegrationTest(SharedSessionTestCase):
    SCENARIO = "min3clos"
    ENV_RUN_ARGS: ClassVar[list[str]] = []

    def _runtime(self):
        return runtime_for_session(self._session_row(self.session_id))

    def test_session_uses_containerlab_backend(self) -> None:
        row = self._session_row(self.session_id)
        assert resolve_backend(row) == "containerlab"
        assert self.SCENARIO in row["lab_name"]
        assert row.get("topology_file") is not None

    def test_all_nodes_deployed(self) -> None:
        nodes = set(self._runtime().list_nodes())
        for name in EXPECTED_NODES:
            assert name in nodes, f"Expected node {name!r} in deployed lab"

    def test_cross_leaf_ping_from_client1(self) -> None:
        import time

        runtime = self._runtime()
        deadline = time.monotonic() + 90.0
        output = ""
        while time.monotonic() < deadline:
            output = runtime.exec(CLIENT1, f"ping -c 1 -W 2 {CLIENT2_IP}", timeout=10)
            if "1 received" in output:
                return
            time.sleep(2.0)
        assert "1 received" in output, f"client1 -> client2 ping failed: {output!r}"

    def test_full_verification_checks_both_client_paths(self) -> None:
        result = verify_min3clos_lab(self._runtime(), scenario_name=self.SCENARIO)
        assert_verify_success(result)
        assert result["checks"]["client_gateways_reachable"]
        assert result["checks"]["cross_leaf_clients_reachable"]

    def test_full_verification_refreshes_after_client_route_failure(self) -> None:
        runtime = self._runtime()
        route = f"blackhole {CLIENT2_IP}/32"
        assert not runtime.exec(CLIENT1, f"ip -4 route show exact {CLIENT2_IP}/32")
        assert_verify_success(verify_min3clos_lab(runtime, scenario_name=self.SCENARIO))
        try:
            runtime.exec(CLIENT1, f"ip -4 route add {route}")
            broken = verify_min3clos_lab(runtime, scenario_name=self.SCENARIO)
            assert broken["verified"] is False
            assert broken["checks"]["cross_leaf_clients_reachable"] is False
        finally:
            runtime.exec(CLIENT1, f"ip -4 route del {route}")
        assert_verify_success(verify_min3clos_lab(runtime, scenario_name=self.SCENARIO))
