from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock
from nika.net_env.probe_intent import ProbeIntent
from nika.service.pingmesh.engine import (
    _build_pairs,
    run_pingmesh_snapshot,
    snapshot_to_json,
)

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
        snapshot = await run_pingmesh_snapshot(api, include_healthy=True)

        assert snapshot["summary"]["anomaly_count"] == 0

        assert snapshot["summary"]["total_pairs"] == 2

        assert all((row["status"] == "ok" for row in snapshot["results"]))

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

    async def test_healthy_rows_omitted_by_default(self) -> None:
        api = self._make_api()
        api.exec_cmd_async = AsyncMock(return_value=OK_OUTPUT)
        snapshot = await run_pingmesh_snapshot(api)

        assert "results" not in snapshot
        assert snapshot["summary"]["total_pairs"] == 2
        assert snapshot["summary"]["rtt_ms"]["max"] == 0.062
        assert list(snapshot)[:3] == ["timestamp", "summary", "anomalies"]

    async def test_cap_is_reported(self) -> None:
        api = MagicMock()
        api.hosts = ["pc1", "pc2", "pc3"]
        api.servers = {}
        api.get_host_ip.side_effect = lambda name: f"10.0.0.{name[-1]}"
        api.exec_cmd_async = AsyncMock(return_value=OK_OUTPUT)
        capped = await run_pingmesh_snapshot(api, max_pairs=2)
        full = await run_pingmesh_snapshot(api)

        assert capped["summary"]["candidate_pairs"] == 6
        assert capped["summary"]["capped"] is True
        assert full["summary"]["capped"] is False

    async def test_single_pair_timeout_does_not_fail_snapshot(self) -> None:
        api = self._make_api()

        async def _exec(host: str, _cmd: str, **_kwargs) -> str:
            if host == "pc1":
                return "[TIMEOUT] Command 'ping' on 'pc1' exceeded 16s."
            return OK_OUTPUT

        api.exec_cmd_async = AsyncMock(side_effect=_exec)
        snapshot = await run_pingmesh_snapshot(api, include_healthy=True)

        rows = {(r["source"], r["target"]): r for r in snapshot["results"]}
        assert rows[("pc1", "pc2")]["status"] == "timeout"
        assert rows[("pc2", "pc1")]["status"] == "ok"
        (anomaly,) = snapshot["anomalies"]
        assert anomaly["type"] == "unknown"
        assert "timed out" in anomaly["detail"]

    async def test_exec_exception_and_unresolvable_endpoint_are_per_pair(self) -> None:
        api = MagicMock()
        api.hosts = ["pc1", "pc2", "pc3"]
        api.servers = {}

        def _ip(name: str) -> str:
            if name == "pc3":
                raise RuntimeError("exec timed out")
            return f"10.0.0.{name[-1]}"

        api.get_host_ip.side_effect = _ip

        async def _exec(host: str, _cmd: str, **_kwargs) -> str:
            if host == "pc2":
                raise ValueError("container paused")
            return OK_OUTPUT

        api.exec_cmd_async = AsyncMock(side_effect=_exec)
        snapshot = await run_pingmesh_snapshot(api, include_healthy=True)

        status = {(r["source"], r["target"]): r["status"] for r in snapshot["results"]}
        assert status[("pc1", "pc2")] == "ok"
        assert status[("pc2", "pc1")] == "error"
        assert status[("pc1", "pc3")] == "unknown"

    async def test_isolated_pair_is_not_an_anomaly_until_reachable(self) -> None:
        api = self._make_api()
        intent = ProbeIntent.from_partitions([["pc1"], ["pc2"]])
        api.exec_cmd_async = AsyncMock(return_value=DOWN_OUTPUT)
        blocked = await run_pingmesh_snapshot(api, intent=intent)

        assert blocked["summary"]["anomaly_count"] == 0
        assert blocked["summary"]["isolated_confirmed"] == 2

        api.exec_cmd_async = AsyncMock(return_value=OK_OUTPUT)
        leaked = await run_pingmesh_snapshot(api, intent=intent)

        assert leaked["summary"]["policy_violation"] == 2
        assert {a["type"] for a in leaked["anomalies"]} == {"policy_violation"}

    async def test_design_rtt_raises_latency_threshold(self) -> None:
        api = self._make_api()
        api.exec_cmd_async = AsyncMock(return_value=HIGH_LATENCY_OUTPUT)
        intent = ProbeIntent.from_partitions(max_expected_rtt_ms=160)
        snapshot = await run_pingmesh_snapshot(api, intent=intent)

        assert snapshot["summary"]["latency_threshold_ms"] == 240
        assert snapshot["summary"]["high_latency"] == 0

    async def test_full_failure_stays_under_tool_output_limit(self) -> None:
        names = [f"pc{i:02d}" for i in range(40)]
        api = MagicMock()
        api.hosts = names
        api.servers = {}
        api.get_host_ip.side_effect = lambda name: f"10.{name[2:]}.0.2"
        api.exec_cmd_async = AsyncMock(return_value=DOWN_OUTPUT)
        snapshot = await run_pingmesh_snapshot(api, include_healthy=False)

        assert len(snapshot_to_json(snapshot)) < 10_000
        assert snapshot["anomalies_omitted"] > 0

    async def test_default_healthy_rows_fit_tool_output_limit(self) -> None:
        names = [f"host_{i:02d}" for i in range(40)]
        api = MagicMock()
        api.hosts = names
        api.servers = {}
        api.get_host_ip.side_effect = lambda name: f"10.{name[-2:]}.0.2"
        api.exec_cmd_async = AsyncMock(return_value=OK_OUTPUT)
        snapshot = await run_pingmesh_snapshot(api, include_healthy=True)

        assert snapshot["summary"]["total_pairs"] == 64
        assert len(snapshot_to_json(snapshot)) < 10_000


class BuildPairsTest:
    def test_sampling_covers_every_source_before_repeating(self) -> None:
        names = [f"h{i}" for i in range(8)]
        pairs, candidates = _build_pairs(names, names, 8)

        assert candidates == 56
        assert sorted(src for src, _ in pairs) == sorted(names)
        assert len({dst for _, dst in pairs}) == 8

    def test_isolated_pairs_get_a_bounded_share(self) -> None:
        names = ["a1", "a2", "b1", "b2"]
        intent = ProbeIntent.from_partitions([["a1", "a2"], ["b1", "b2"]])
        pairs, candidates = _build_pairs(
            names, names, 4, is_isolated=intent.isolation_matcher()
        )
        isolated = [p for p in pairs if intent.isolation_matcher()(*p)]

        assert candidates == 12
        assert len(pairs) == 4
        assert len(isolated) == 1

    def test_unused_isolated_budget_flows_to_expected_pairs(self) -> None:
        names = ["a", "b", "c"]
        pairs, _ = _build_pairs(names, names, 6)

        assert len(pairs) == 6
