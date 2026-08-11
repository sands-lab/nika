"""Docker integration tests for isp (real Kathara deploy).

Large SNDlib graphs (e.g. brain, ta2) need substantial Docker CPU/memory and
can take many minutes each. Tests run one topology at a time and use the
scenario's adaptive VERIFY_MAX_WAIT_SEC.

Coverage:
- Every catalog topology × IS-IS and OSPF (BGP off); edge stubs always present
- Every catalog topology × ibgp_rr and ebgp (IGP defaults to IS-IS)
- Traffic: pdh/polska/abilene × demands; polska/abilene × dynamic (fixture cache)
"""

from __future__ import annotations

import pytest

from nika.net_env.isp.bgp import compile_bgp_plan
from nika.net_env.isp.igp import IspConfig, compile_isp_plan
from nika.net_env.kathara.isp.isp.lab import Isp
from nika.net_env.net_env_pool import get_net_env_instance
from nika.net_env.verify import build_lab_verify_result
from nika.runtime.factory import resolve_backend, runtime_for_session
from nika.topology import list_sndlib_topologies, load_sndlib_topology
from nika.utils.session_id import resolve_session_tag
from nika.workflows.env.start import start_net_env
from tests.support.integration_base import IntegrationTestCase
from tests.support.net_env import assert_verify_success
from tests.support.prerequisites import docker_available

ALL_TOPOS = list_sndlib_topologies()
ALL_IGPS = ("isis", "ospf")
ALL_BGP_MODES = ("ibgp_rr", "ebgp")


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class IspDockerTest(IntegrationTestCase):
    @pytest.mark.parametrize("igp", ALL_IGPS)
    @pytest.mark.parametrize("topo_name", ALL_TOPOS)
    def test_topo_starts_verifies_and_destroys(self, topo_name: str, igp: str) -> None:
        ir = load_sndlib_topology(topo_name)
        plan = compile_isp_plan(
            IspConfig(topology=topo_name, igp=igp)  # type: ignore[arg-type]
        )
        session_id = self._start_env(
            "isp",
            ["--topo", topo_name, "--igp", igp],
        )
        lab_name = None
        try:
            row = self._assert_session_ready(session_id, "isp")
            lab_name = row["lab_name"]
            assert resolve_backend(row) == "kathara"
            params = row.get("scenario_params") or {}
            assert params.get("topo") == topo_name
            assert params.get("igp") == igp
            assert params.get("metric_strategy") == "constant"
            assert params.get("bgp_mode") == "none"

            runtime = runtime_for_session(row)
            nodes = set(runtime.list_nodes())
            routers = {n for n in nodes if not n.startswith("pc_")}
            stubs = {n for n in nodes if n.startswith("pc_")}
            assert len(routers) == len(ir.nodes) == plan.inventory["node_count"]
            assert len(stubs) == len(ir.nodes)
            for node in plan.nodes:
                assert node.device_name in routers
                assert f"pc_{node.device_name}" in stubs

            # Re-verify with live Isp instance (includes stub host checks).
            env = get_net_env_instance(
                "isp",
                topo=topo_name,
                igp=igp,
                lab_name=lab_name,
            )
            result = env.verify_lab()
            assert_verify_success(result)
            assert result["details"]["inventory"]["link_count"] == len(ir.links)
            assert result["details"]["igp"] == igp
            assert result["details"]["bgp_mode"] == "none"
            assert result["details"]["traffic_stubs"] is True
            assert result["checks"].get("stub_gateway_reachable") is True
        finally:
            self._close_session(session_id)
            if lab_name:
                env = get_net_env_instance(
                    "isp",
                    topo=topo_name,
                    igp=igp,
                    lab_name=lab_name,
                )
                assert not env.lab_exists()

    @pytest.mark.parametrize("bgp_mode", ALL_BGP_MODES)
    @pytest.mark.parametrize("topo_name", ALL_TOPOS)
    def test_bgp_starts_verifies_and_destroys(
        self, topo_name: str, bgp_mode: str
    ) -> None:
        isp_plan = compile_isp_plan(IspConfig(topology=topo_name))
        bgp_plan = compile_bgp_plan(isp_plan, bgp_mode)
        assert bgp_plan is not None
        session_id = self._start_env(
            "isp",
            ["--topo", topo_name, "--igp", "isis", "--bgp-mode", bgp_mode],
        )
        lab_name = None
        try:
            row = self._assert_session_ready(session_id, "isp")
            lab_name = row["lab_name"]
            params = row.get("scenario_params") or {}
            assert params.get("bgp_mode") == bgp_mode
            env = get_net_env_instance(
                "isp",
                topo=topo_name,
                igp="isis",
                bgp_mode=bgp_mode,
                lab_name=lab_name,
            )
            result = env.verify_lab()
            assert_verify_success(result)
            assert result["checks"]["bgp_sessions"]
            assert result["checks"]["bgp_prefixes_propagated"]
            assert result["checks"]["bgp_infra_denied"]
            assert result["details"]["bgp_mode"] == bgp_mode
            assert result["details"]["traffic_stubs"] is True
        finally:
            self._close_session(session_id)
            if lab_name:
                env = get_net_env_instance(
                    "isp",
                    topo=topo_name,
                    igp="isis",
                    bgp_mode=bgp_mode,
                    lab_name=lab_name,
                )
                assert not env.lab_exists()

    def test_verify_failure_cleans_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original_init = Isp.__init__

        def short_verify_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            self.VERIFY_MAX_WAIT_SEC = 0
            self.VERIFY_RETRY_DELAY_SEC = 0

        def failing_verify(self):
            return build_lab_verify_result(
                scenario_name=self.LAB_NAME,
                verified=False,
                checks={"forced_failure": False},
            )

        monkeypatch.setattr(Isp, "__init__", short_verify_init)
        monkeypatch.setattr(Isp, "verify_lab", failing_verify)

        lab_name_box: dict[str, str] = {}
        original_get = get_net_env_instance

        def tracking_get(scenario_name: str, **kwargs):
            env = original_get(scenario_name, **kwargs)
            lab_name_box["lab_name"] = env.name
            return env

        monkeypatch.setattr(
            "nika.workflows.env.start.get_net_env_instance", tracking_get
        )

        with pytest.raises(RuntimeError, match="Lab verification failed"):
            start_net_env(
                "isp",
                None,
                topo="pdh",
                igp="isis",
                session_tag=resolve_session_tag(context="test"),
            )

        assert "lab_name" in lab_name_box
        env = original_get(
            "isp",
            topo="pdh",
            igp="isis",
            lab_name=lab_name_box["lab_name"],
        )
        assert not env.lab_exists()


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class IspTrafficCompatDockerTest(IntegrationTestCase):
    """Several topos × static demands and fixture-backed dynamic replay.

    Assertions require real iperf3 processes on stub hosts during replay, not
    just a non-empty return payload.
    """

    TRAFFIC_TOPOS = ("pdh", "polska", "abilene")

    def _tiny_series(
        self, series, *, n_flows: int = 3, duration_sec: int = 8, max_intervals: int = 1
    ):
        from nika.net_env.isp.traffic.models import TrafficInterval, TrafficMatrixSeries

        intervals = []
        for i, src in enumerate(series.intervals[:max_intervals]):
            take = n_flows if i == 0 else max(1, n_flows // 2)
            flows = src.flows[:take] or series.intervals[0].flows[:1]
            assert flows, f"expected flows for interval {i}"
            intervals.append(
                TrafficInterval(index=i, duration_sec=duration_sec, flows=flows)
            )
        return TrafficMatrixSeries(
            topology=series.topology,
            source=series.source,
            intervals=tuple(intervals),
            sample_period_sec=duration_sec,
            unit_note=series.unit_note,
            path=series.path,
        )

    def _hosts_in_od(self, od: dict[str, dict[str, int]]) -> set[str]:
        hosts = set(od)
        for dests in od.values():
            hosts.update(dests)
        return hosts

    def _iperf_hosts(self, runtime, hosts: set[str]) -> set[str]:
        alive: set[str] = set()
        for host in hosts:
            out = (
                runtime.exec(host, "pgrep -a iperf3 || true", timeout=15) or ""
            ).strip()
            if "iperf3" in out:
                alive.add(host)
        return alive

    def _replay_and_assert_live(
        self,
        *,
        row: dict,
        topo_name: str,
        mode: str,
        scale: float = 1.0,
        max_intervals: int = 1,
        cache_root=None,
        n_flows: int = 3,
    ) -> list:
        import time

        from nika.generator.traffic.od_flows import ODFLowGenerator
        from nika.net_env.isp.traffic import resolve_traffic_series, series_to_od_dicts

        env = get_net_env_instance(
            "isp",
            topo=topo_name,
            igp="isis",
            lab_name=row["lab_name"],
        )
        kwargs = {}
        if cache_root is not None:
            kwargs["cache_root"] = cache_root
        resolved = resolve_traffic_series(topo_name, mode, **kwargs)
        assert resolved is not None
        assert resolved.source == ("dynamic" if mode == "dynamic" else "demands")
        series = self._tiny_series(
            resolved,
            n_flows=n_flows,
            duration_sec=8,
            max_intervals=max_intervals,
        )

        runtime = runtime_for_session(row)
        od_list = series_to_od_dicts(series, scale=scale, inventory=env.inventory)
        results: list[dict] = []
        odg = ODFLowGenerator(runtime)

        for index, (interval, od) in enumerate(zip(series.intervals, od_list)):
            assert od, f"interval {index} produced empty OD"
            hosts = self._hosts_in_od(od)
            for host in hosts:
                runtime.exec(host, "pkill -9 iperf3 || true", timeout=10)
            time.sleep(0.3)
            assert not self._iperf_hosts(runtime, hosts), (
                "iperf3 still alive after pkill"
            )

            labels = odg.start_traffic_background(
                od,
                interval=interval.duration_sec,
                unit="K",
                udp=True,
            )
            assert labels, f"interval {index}: no background flow labels"
            time.sleep(1.5)
            alive = self._iperf_hosts(runtime, hosts)
            assert alive, (
                f"interval {index}: no iperf3 processes on {sorted(hosts)} "
                f"after starting {labels}"
            )
            client_seen = False
            for host in alive:
                out = runtime.exec(host, "pgrep -a iperf3 || true", timeout=10) or ""
                if "iperf3 -c " in out:
                    client_seen = True
                    break
            assert client_seen, f"interval {index}: no iperf3 client (-c) among {alive}"

            time.sleep(max(0.5, interval.duration_sec - 1.5))
            results.append(
                {
                    "index": index,
                    "background": True,
                    "labels": labels,
                    "duration_sec": interval.duration_sec,
                    "flow_pairs": sum(len(v) for v in od.values()),
                    "iperf_hosts": sorted(alive),
                }
            )
        return results

    def _write_dynamic_fixture(self, topo: str, cache_root) -> None:
        from nika.net_env.isp.traffic import (
            dynamic_cache_dir,
            series_from_demands,
            write_normalized_series,
        )
        from nika.net_env.isp.traffic.models import TrafficInterval, TrafficMatrixSeries
        from nika.topology import load_sndlib_topology

        base = series_from_demands(load_sndlib_topology(topo), duration_sec=5)
        flows = base.intervals[0].flows[:4]
        series = TrafficMatrixSeries(
            topology=topo,
            source="dynamic",
            intervals=(
                TrafficInterval(index=0, duration_sec=5, flows=flows),
                TrafficInterval(index=1, duration_sec=5, flows=flows[:2] or flows),
            ),
            sample_period_sec=5,
            unit_note="docker fixture",
        )
        write_normalized_series(series, dynamic_cache_dir(topo, cache_root=cache_root))

    @pytest.mark.parametrize("topo_name", TRAFFIC_TOPOS)
    def test_demands_replay(self, topo_name: str) -> None:
        session_id = self._start_env(
            "isp",
            ["--topo", topo_name, "--igp", "isis"],
        )
        try:
            row = self._assert_session_ready(session_id, "isp")
            runtime = runtime_for_session(row)
            assert any(n.startswith("pc_") for n in runtime.list_nodes())

            env = get_net_env_instance(
                "isp",
                topo=topo_name,
                igp="isis",
                lab_name=row["lab_name"],
            )
            result = env.verify_lab()
            assert_verify_success(result)
            assert result["details"]["traffic_stubs"] is True

            payload = self._replay_and_assert_live(
                row=row,
                topo_name=topo_name,
                mode="demands",
                scale=0.5,
                max_intervals=1,
            )
            assert payload[0]["index"] == 0
            assert payload[0]["flow_pairs"] >= 1
            assert payload[0]["iperf_hosts"]
        finally:
            self._close_session(session_id)

    @pytest.mark.parametrize("topo_name", ("polska", "abilene"))
    def test_dynamic_fixture_replay(self, topo_name: str, tmp_path) -> None:
        cache_root = tmp_path / ".nika_cache"
        self._write_dynamic_fixture(topo_name, cache_root)

        session_id = self._start_env(
            "isp",
            ["--topo", topo_name, "--igp", "isis"],
        )
        try:
            row = self._assert_session_ready(session_id, "isp")
            payload = self._replay_and_assert_live(
                row=row,
                topo_name=topo_name,
                mode="dynamic",
                max_intervals=2,
                cache_root=cache_root,
            )
            assert len(payload) == 2
            assert payload[0]["index"] == 0
            assert payload[1]["index"] == 1
            assert all(p["flow_pairs"] >= 1 and p["iperf_hosts"] for p in payload)
        finally:
            self._close_session(session_id)
