from __future__ import annotations

import pytest
from nika.net_env.kathara.data_center_routing.dc_clos_bgp.verify import (
    verify_dc_clos_bgp_lab,
    verify_dc_clos_service_lab,
)
from nika.net_env.kathara.interdomain_routing.simple_bgp.verify import (
    verify_simple_bgp_lab,
)
from nika.net_env.kathara.intradomain_routing.rip_vpn.verify import verify_rip_vpn_lab
from nika.net_env.kathara.kubernetes.k8s_lab.verify import verify_k8s_lab
from nika.net_env.kathara.kubernetes.llmd_lab.verify import verify_llmd_lab
from nika.net_env.kathara.p4.p4_bloom_filter.verify import verify_p4_bloom_filter_lab
from nika.net_env.kathara.p4.p4_counter.verify import verify_p4_counter_lab
from nika.net_env.kathara.p4.p4_int.verify import verify_p4_int_lab
from nika.net_env.kathara.p4.p4_mpls.verify import verify_p4_mpls_lab
from nika.net_env.kathara.sdn.verify import verify_sdn_clos_lab, verify_sdn_star_lab
from nika.runtime.factory import resolve_backend, runtime_for_session
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available
from tests.support.net_env import assert_verify_success

ALL_NODES = {
    "router1",
    "router2",
    "gateway_router",
    "external_router_1",
    "pc1",
    "pc2",
    "pc3",
    "vpn_server_1",
    "web_server_1_1",
    "super_spine_router_0",
    "spine_router_0_0",
    "spine_router_0_1",
    "leaf_router_0_0",
    "leaf_router_0_1",
    "pc_0_0",
    "pc_0_1",
    "dns_pod0",
    "webserver0_pod0",
    "client_0",
    "controller",
    "switch_0",
    "switch_1",
    "switch_2",
    "spine_1",
    "leaf_1",
    "leaf_2",
    "pc_1_1",
    "pc_2_1",
    "s1",
    "s2",
    "s3",
    "s4",
    "collector",
    "leaf1",
    "leaf2",
    "spine1",
    "spine2",
    "switch_3",
    "switch_4",
    "switch_5",
    "switch_6",
    "switch_7",
    "worker1",
    "worker2",
    "worker3",
    "worker4",
    "worker5",
    "client",
    "leaf_1_1",
}
HOST_ADDRS = {
    "pc1": ("195.11.14.2", "10.0.0.1", "10.1.1.2", "10.0.0.2"),
    "pc2": ("200.1.1.2", "10.0.0.2", "10.7.2.2", "10.0.1.2"),
    "pc3": ("10.0.0.3", "10.7.3.2"),
    "pc_0_0": ("10.0.0.2",),
    "pc_0_1": ("10.0.1.2",),
    "client_0": ("192.168.0.2",),
    "pc_1_1": ("10.0.0.1",),
    "pc_2_1": ("10.0.0.3",),
    "collector": ("10.0.0.3",),
    "controller": ("201.1.1.2", "200.0.0.1"),
    "client": ("3.0.0.2", "200.0.0.7"),
}


class FakeRuntime:
    def __init__(
        self,
        *,
        nodes: set[str] | None = None,
        overrides: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self.nodes = nodes or ALL_NODES
        self.overrides = overrides or {}

    def list_nodes(self) -> list[str]:
        return sorted(self.nodes)

    def exec(self, host: str, command: str, timeout: float = 10.0) -> str:
        if (host, command) in self.overrides:
            return self.overrides[host, command]
        if command.startswith("ping -c 3"):
            return "3 packets received"
        if command.startswith("ping -c 1"):
            return "1 received"
        if command.startswith("cat /sys/class/net/"):
            return "up"
        if command.startswith("ip -4 -o addr show"):
            return "\n".join((f"inet {addr}/24" for addr in HOST_ADDRS.get(host, ())))
        if command == "ip route show default":
            return "default via 195.11.14.1 via 10.0.0.1"
        if command == "systemctl is-active frr":
            return "active"
        if command in {"systemctl is-active named", "systemctl is-active apache2"}:
            return "active"
        if command == "pgrep -x simple_switch":
            return "123"
        if command == "pgrep -x python3":
            return "456"
        if command == "ovs-vsctl show":
            return "Bridge br0"
        if command == "vtysh -c 'show bgp summary'":
            return "eth0 4 65000 1\neth1 4 65001 2\n"
        if command == "wg show wg0":
            return "interface: wg0"
        if command.startswith("curl -s -o /dev/null"):
            return "200"
        if command == "kubectl get nodes --no-headers":
            return "\n".join(
                (f"node{idx} Ready control-plane 1m v1.0" for idx in range(6))
            )
        if "jsonpath={.status.loadBalancer.ingress[0].ip}" in command:
            return "101.0.0.1"
        if command == "kubectl get pods -n metallb-system --no-headers":
            return "speaker Running"
        if command.startswith("kubectl get gateway -n llm-d llm-d-gateway"):
            return "200.0.0.240"
        if command == "kubectl get pods -n agentgateway-system --no-headers":
            return "agentgateway Running"
        if command == "kubectl get gateway -A --no-headers":
            return "default pd-gateway"
        return ""


class KatharaVerifyUnitTest:
    def assert_verified(self, result: dict) -> None:
        assert_verify_success(result)

    def test_simple_bgp_verify_passes(self) -> None:
        assert_verify_success(verify_simple_bgp_lab(FakeRuntime(), scenario_name="x"))

    def test_dc_clos_bgp_verify_passes(self) -> None:
        assert_verify_success(verify_dc_clos_bgp_lab(FakeRuntime(), scenario_name="x"))

    def test_dc_clos_service_verify_passes(self) -> None:
        assert_verify_success(
            verify_dc_clos_service_lab(FakeRuntime(), scenario_name="x")
        )

    def test_rip_vpn_verify_passes(self) -> None:
        assert_verify_success(verify_rip_vpn_lab(FakeRuntime(), scenario_name="x"))

    def test_sdn_star_verify_passes(self) -> None:
        assert_verify_success(verify_sdn_star_lab(FakeRuntime(), scenario_name="x"))

    def test_sdn_clos_verify_passes(self) -> None:
        assert_verify_success(verify_sdn_clos_lab(FakeRuntime(), scenario_name="x"))

    def test_p4_bloom_filter_verify_passes(self) -> None:
        assert_verify_success(
            verify_p4_bloom_filter_lab(FakeRuntime(), scenario_name="x")
        )

    def test_p4_counter_verify_passes(self) -> None:
        assert_verify_success(verify_p4_counter_lab(FakeRuntime(), scenario_name="x"))

    def test_p4_int_verify_passes(self) -> None:
        assert_verify_success(verify_p4_int_lab(FakeRuntime(), scenario_name="x"))

    def test_p4_mpls_verify_passes(self) -> None:
        assert_verify_success(verify_p4_mpls_lab(FakeRuntime(), scenario_name="x"))

    def test_k8s_verify_passes(self) -> None:
        assert_verify_success(verify_k8s_lab(FakeRuntime(), scenario_name="x"))

    def test_llmd_verify_passes(self) -> None:
        assert_verify_success(verify_llmd_lab(FakeRuntime(), scenario_name="x"))

    def test_missing_node_fails(self) -> None:
        result = verify_simple_bgp_lab(
            FakeRuntime(nodes=ALL_NODES - {"pc2"}), scenario_name="x"
        )

        assert not result["verified"]

        assert not result["checks"]["nodes_deployed"]

    def test_p4_process_failure_fails(self) -> None:
        result = verify_p4_bloom_filter_lab(
            FakeRuntime(overrides={("switch_1", "pgrep -x simple_switch"): ""}),
            scenario_name="x",
        )

        assert not result["verified"]

        assert not result["checks"]["p4_switches_ready"]

    def test_k8s_not_ready_fails(self) -> None:
        result = verify_k8s_lab(
            FakeRuntime(
                overrides={("controller", "kubectl get nodes --no-headers"): ""}
            ),
            scenario_name="x",
        )

        assert not result["verified"]

        assert not result["checks"]["k3s_nodes_ready"]


SCENARIO_CASES: tuple[tuple[str, list[str], tuple[str, ...]], ...] = (
    ("simple_bgp", [], ("router1", "router2", "pc1", "pc2")),
    (
        "dc_clos_bgp",
        ["-s", "s"],
        (
            "super_spine_router_0",
            "spine_router_0_0",
            "leaf_router_0_0",
            "pc_0_0",
            "pc_0_1",
        ),
    ),
    (
        "dc_clos_service",
        ["-s", "s"],
        (
            "super_spine_router_0",
            "spine_router_0_0",
            "leaf_router_0_0",
            "dns_pod0",
            "webserver0_pod0",
            "client_0",
        ),
    ),
    (
        "rip_small_internet_vpn",
        ["-s", "s"],
        (
            "router1",
            "router2",
            "gateway_router",
            "external_router_1",
            "pc1",
            "vpn_server_1",
            "web_server_1_1",
        ),
    ),
    ("sdn_star", ["-s", "s"], ("controller", "switch_0", "switch_1", "pc1", "pc2")),
    (
        "sdn_clos",
        ["-s", "s"],
        ("controller", "spine_1", "leaf_1", "leaf_2", "pc_1_1", "pc_2_1"),
    ),
    ("p4_bloom_filter", [], ("pc1", "pc2", "switch_1", "switch_2")),
    ("p4_counter", [], ("pc1", "pc2", "pc3", "s1", "s2", "s3", "s4")),
    ("p4_int", [], ("pc1", "pc2", "collector", "leaf1", "leaf2", "spine1", "spine2")),
    ("p4_mpls", [], ("pc1", "pc2", "pc3", "switch_1", "switch_4", "switch_7")),
)


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class KatharaScenarioVerifyIntegrationTest(IntegrationTestCase):
    def test_scenarios_start_and_verify(self) -> None:
        for scenario, args, expected_nodes in SCENARIO_CASES:
            session_id = self._start_env(scenario, args)
            try:
                row = self._assert_session_ready(session_id, scenario)

                assert resolve_backend(row) == "kathara"
                nodes = set(runtime_for_session(row).list_nodes())
                for node in expected_nodes:
                    assert node in nodes
            finally:
                self._close_session(session_id)
