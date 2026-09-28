from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock
from nika.service.pingmesh.engine import run_pingmesh_snapshot

pytestmark = pytest.mark.unit

OK_OUTPUT = "\n4 packets transmitted, 4 received, 0% packet loss, time 3005ms\nrtt min/avg/max/mdev = 0.045/0.062/0.089/0.018 ms\n"
DOWN_OUTPUT = "\n4 packets transmitted, 0 received, 100% packet loss, time 3050ms\n"
HIGH_LATENCY_OUTPUT = "\n4 packets transmitted, 4 received, 0% packet loss, time 3005ms\nrtt min/avg/max/mdev = 120.0/150.0/180.0/10.0 ms\n"


class RunPingmeshSnapshotTest:
    def _make_api(self) -> MagicMock:
        api = MagicMock()
        api.hosts = ["pc1", "pc2"]
        api.servers = {}
        api.get_host_ip.side_effect = lambda name: {
            "pc1": "10.0.0.1",
            "pc2": "10.0.0.2",
        }[name]
        return api

    async def test_healthy_snapshot(self) -> None:
        api = self._make_api()
        api.exec_cmd_async = AsyncMock(return_value=OK_OUTPUT)
        snapshot = await run_pingmesh_snapshot(api)

        assert snapshot["summary"]["anomaly_count"] == 0

        assert snapshot["summary"]["total_pairs"] == 2

        assert all((row["reachable"] for row in snapshot["results"]))

    async def test_unreachable_pair(self) -> None:
        api = self._make_api()

        async def _exec(host: str, _cmd: str, **_kwargs) -> str:
            if host == "pc1":
                return DOWN_OUTPUT
            return OK_OUTPUT

        api.exec_cmd_async = AsyncMock(side_effect=_exec)
        snapshot = await run_pingmesh_snapshot(api)

        assert snapshot["summary"]["unreachable"] > 0
        anomaly_pairs = {(a["source"], a["target"]) for a in snapshot["anomalies"]}

        assert ("pc1", "pc2") in anomaly_pairs

    async def test_high_latency_anomaly(self) -> None:
        api = self._make_api()
        api.exec_cmd_async = AsyncMock(return_value=HIGH_LATENCY_OUTPUT)
        snapshot = await run_pingmesh_snapshot(api, high_latency_ms=100.0)

        assert snapshot["summary"]["high_latency"] > 0

    async def test_unknown_source_rejected(self) -> None:
        api = self._make_api()
        with pytest.raises(ValueError, match="Unknown sources"):
            await run_pingmesh_snapshot(api, sources=["router1"])

    async def test_max_pairs_limit(self) -> None:
        api = MagicMock()
        api.hosts = ["pc1", "pc2", "pc3"]
        api.servers = {}
        api.get_host_ip.side_effect = lambda name: f"10.0.0.{name[-1]}"
        api.exec_cmd_async = AsyncMock(return_value=OK_OUTPUT)
        snapshot = await run_pingmesh_snapshot(api, max_pairs=2)

        assert snapshot["summary"]["total_pairs"] == 2

    async def test_no_endpoints_raises(self) -> None:
        api = MagicMock()
        api.hosts = []
        api.servers = {}
        with pytest.raises(ValueError, match="No endpoint hosts"):
            await run_pingmesh_snapshot(api)
