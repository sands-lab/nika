"""Containerlab ISP smoke for representative SNDlib topologies (Nokia SR Linux).

Covers: verify → semantic tools → tiny data-plane traffic → inject → destroy.
Requires Docker + clab + gnmic. Large matrices intentionally truncated.
"""

from __future__ import annotations

import ipaddress
import subprocess
import time

import pytest

from traffic.od_flows import ODFLowGenerator
from traffic.sndlib_replay import host_ips_from_isp_inventory
from nika.net_env.isp.inject_targets import isp_inject_params
from nika.net_env.isp.traffic import resolve_traffic_series, series_to_od_dicts
from nika.net_env.isp.traffic.models import TrafficInterval, TrafficMatrixSeries
from nika.net_env.net_env_pool import get_net_env_instance
from nika.runtime.factory import resolve_backend, runtime_for_session
from tests.support.integration_base import CliIntegrationTestCase
from tests.support.net_env import assert_verify_success
from tests.support.prerequisites import containerlab_prerequisites

DATA_PLANE = ipaddress.ip_network("10.254.0.0/16")
# geant (22 SRL) is intentionally omitted: ~15Gi hosts routinely stall/OOM.
REPR_TOPOS = ("pdh", "polska", "abilene")
# Full demands matrices are huge; only exercise the CLI path on the tiny topo.
CLI_TRAFFIC_TOPOS = ("pdh",)


def _stage(topo: str, msg: str) -> None:
    """Unbuffered progress so long clab deploy/verify does not look hung."""
    print(f"[isp-clab:{topo}] {msg}", flush=True)


@pytest.mark.skipif(
    not containerlab_prerequisites(), reason="Containerlab/gnmic not available"
)
class IspClabReprSmokeTest(CliIntegrationTestCase):
    """Live lab per representative topo: verify → tools → traffic → inject."""

    def _env_args(self, topo: str) -> list[str]:
        return [
            "--backend",
            "containerlab",
            "--device-profile",
            "nokia_srlinux",
            "--topo",
            topo,
            "--igp",
            "isis",
            "--bgp-mode",
            "ibgp_rr",
        ]

    def _get_env(self, row: dict, topo: str):
        return get_net_env_instance(
            "isp",
            backend="containerlab",
            topo=topo,
            igp="isis",
            bgp_mode="ibgp_rr",
            device_profile="nokia_srlinux",
            lab_name=row["lab_name"],
            topology_file=row.get("topology_file"),
            runtime_workdir=row.get("runtime_workdir"),
        )

    def _assert_semantic_tools(self, runtime, env) -> None:
        nodes = runtime.list_nodes()
        assert nodes, "expected deployed nodes"
        stubs = sorted(n for n in nodes if n.startswith("pc_"))
        routers = sorted(n for n in nodes if not n.startswith("pc_"))
        assert stubs, "expected edge stub hosts"
        assert routers, "expected routers"

        stub = stubs[0]
        out = runtime.exec(stub, "hostname || true", timeout=15) or ""
        assert out.strip(), f"exec failed on {stub}"

        hosts = env.inventory.get("hosts") or []
        assert hosts, "inventory missing hosts"
        host0 = next(h for h in hosts if h["host"] == stub)
        gw = host0["gateway"]
        assert runtime.ping_ok(stub, gw, count=1), (
            f"stub {stub} cannot ping gateway {gw}"
        )
        # Cross-stub reachability is already covered by verify_lab; skip a
        # second multi-hop ping here (can look hung under load).

    def _assert_tiny_traffic(self, runtime, env, topo: str) -> None:
        host_ips = host_ips_from_isp_inventory(env.inventory)
        assert host_ips, "expected stub addresses in inventory"
        for ip in host_ips.values():
            assert ipaddress.ip_address(ip) in DATA_PLANE, ip
            assert not ip.startswith("172.100."), ip

        series = resolve_traffic_series(topo, "demands")
        assert series is not None
        flows = series.intervals[0].flows[:3]
        assert flows
        tiny = TrafficMatrixSeries(
            topology=series.topology,
            source=series.source,
            intervals=(TrafficInterval(index=0, duration_sec=6, flows=flows),),
            sample_period_sec=6,
            unit_note=series.unit_note,
            path=series.path,
        )
        od_list = series_to_od_dicts(tiny, scale=1.0, inventory=env.inventory)
        assert od_list and od_list[0]
        od = od_list[0]

        hosts = set(od)
        for dests in od.values():
            hosts.update(dests)
        for host in hosts:
            runtime.exec(host, "pkill -9 iperf3 || true", timeout=10)
        time.sleep(0.3)

        labels = ODFLowGenerator(runtime).start_traffic_background(
            od,
            interval=6,
            unit="K",
            udp=True,
            host_ips=host_ips,
        )
        assert labels
        time.sleep(1.5)
        client_line = ""
        for host in hosts:
            out = runtime.exec(host, "pgrep -a iperf3 || true", timeout=10) or ""
            if "iperf3 -c " in out:
                client_line = out
                break
        assert client_line, f"no iperf3 client among {sorted(hosts)}"
        assert "172.100." not in client_line, client_line
        assert any(ip in client_line for ip in host_ips.values()), client_line

        time.sleep(5)
        for host in hosts:
            runtime.exec(host, "pkill -9 iperf3 || true", timeout=10)

    def _assert_traffic_cli(self, session_id: str) -> None:
        """Exercise ``nika traffic run sndlib`` on the sole running session."""
        self._invoke_ok(
            [
                "traffic",
                "run",
                "sndlib",
                "--mode",
                "demands",
                "--unit",
                "K",
                "--max-intervals",
                "1",
                "--background",
            ]
        )
        runtime = runtime_for_session(self._session_row(session_id))
        for host in runtime.list_nodes():
            if host.startswith("pc_"):
                runtime.exec(host, "pkill -9 iperf3 || true", timeout=10)

    def _assert_inject(self, session_id: str, env) -> None:
        for problem in ("bgp_asn_misconfig", "link_down"):
            inject = isp_inject_params(
                problem, env.inventory, (env.inventory.get("bgp") or None)
            )
            self._inject_failure(problem, inject, session_id=session_id)
            self._assert_failure_injected(problem, session_id=session_id)

    def _assert_no_leftover_clab(self) -> None:
        names = subprocess.check_output(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            text=True,
        ).splitlines()
        leftover = [n for n in names if n.startswith("clab-isp")]
        assert not leftover, f"leftover containers: {leftover}"

    @pytest.mark.parametrize("topo", REPR_TOPOS)
    def test_repr_topo_verify_tools_traffic_inject(self, topo: str) -> None:
        _stage(topo, "starting env (deploy+gnmi+setup+verify; may take >10min)")
        t0 = time.time()
        session_id = self._start_env("isp", self._env_args(topo))
        _stage(topo, f"env ready in {time.time() - t0:.0f}s session={session_id}")
        try:
            row = self._assert_session_ready(session_id, "isp")
            assert resolve_backend(row) == "containerlab"
            params = row.get("scenario_params") or {}
            assert params.get("topo") == topo
            assert params.get("igp") == "isis"
            assert params.get("bgp_mode") == "ibgp_rr"
            assert params.get("device_profile") == "nokia_srlinux"

            env = self._get_env(row, topo)
            _stage(topo, "re-verify_lab")
            result = env.verify_lab()
            assert_verify_success(result)
            assert result["details"]["bgp_mode"] == "ibgp_rr"
            assert result["details"]["topology_name"] == topo

            runtime = runtime_for_session(row)
            _stage(topo, "semantic tools")
            self._assert_semantic_tools(runtime, env)
            _stage(topo, "tiny traffic")
            self._assert_tiny_traffic(runtime, env, topo)
            if topo in CLI_TRAFFIC_TOPOS:
                _stage(topo, "traffic CLI")
                self._assert_traffic_cli(session_id)
            _stage(topo, "inject")
            self._assert_inject(session_id, env)
            _stage(topo, "done")
        finally:
            _stage(topo, "closing session")
            self._close_session(session_id)
            self._assert_no_leftover_clab()
