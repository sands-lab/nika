from __future__ import annotations

import pytest
from typing import ClassVar
from nika.net_env.min3clos.verify import (
    CLIENT1,
    CLIENT2_IP,
    EXPECTED_NODES,
)
from nika.runtime.factory import resolve_backend, runtime_for_session
from tests.support.integration_base import SharedSessionTestCase
from tests.support.prerequisites import containerlab_prerequisites


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
        runtime = self._runtime()
        output = runtime.exec(CLIENT1, f"ping -c 1 -W 2 {CLIENT2_IP}", timeout=10)
        assert "1 received" in output, f"client1 -> client2 ping failed: {output!r}"
