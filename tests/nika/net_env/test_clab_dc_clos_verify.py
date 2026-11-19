from __future__ import annotations

import pytest
from typing import ClassVar
from nika.runtime.factory import resolve_backend, runtime_for_session
from tests.support.integration_base import SharedSessionTestCase
from tests.support.prerequisites import containerlab_prerequisites
from tests.support.prerequisites import docker_image_available
from nika.net_env.utils.sonic.common import IMAGE as SONIC_IMAGE
from tests.support.net_env import assert_verify_success

CLIENT1 = "client_1_1"
CLIENT2_IP = "10.0.2.11"

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    not containerlab_prerequisites(),
    reason="containerlab, gnmic, or Docker not available",
)
class DCClosVerifyIntegrationTest(SharedSessionTestCase):
    SCENARIO = "dc_clos"
    ENV_RUN_ARGS: ClassVar[list[str]] = ["-s", "s", "--backend", "containerlab"]

    def _env(self):
        from nika.net_env.net_env_pool import get_net_env_instance

        return get_net_env_instance(self.SCENARIO, **self._scenario_kwargs())

    def _runtime(self):
        return runtime_for_session(self._session_row(self.session_id))

    def test_session_uses_containerlab_backend(self) -> None:
        row = self._session_row(self.session_id)
        assert resolve_backend(row) == "containerlab"
        assert self.SCENARIO in row["lab_name"]
        assert row.get("topology_file") is not None

    def test_all_nodes_deployed(self) -> None:
        nodes = set(self._runtime().list_nodes())
        for name in (node.name for node in self._env().get_lab_spec().nodes):
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
        result = self._env().verify_lab()
        assert_verify_success(result)
        assert result["checks"]["dns_servers_reachable"]
        assert result["checks"]["web_http_by_name"]

    def test_full_verification_refreshes_after_client_route_failure(self) -> None:
        runtime = self._runtime()
        route = f"blackhole {CLIENT2_IP}/32"
        assert not runtime.exec(CLIENT1, f"ip -4 route show exact {CLIENT2_IP}/32")
        assert_verify_success(self._env().verify_lab())
        try:
            runtime.exec(CLIENT1, f"ip -4 route add {route}")
            broken = self._env().verify_lab()
            assert broken["verified"] is False
            assert broken["checks"]["web_http_by_name"] is False
        finally:
            runtime.exec(CLIENT1, f"ip -4 route del {route}")
        assert_verify_success(self._env().verify_lab())


@pytest.mark.skipif(
    not docker_image_available(SONIC_IMAGE), reason="SONiC image not available"
)
class DCClosSonicVerifyIntegrationTest(DCClosVerifyIntegrationTest):
    ENV_RUN_ARGS: ClassVar[list[str]] = [
        "-s",
        "s",
        "--backend",
        "containerlab",
        "--device-profile",
        "sonic",
    ]
