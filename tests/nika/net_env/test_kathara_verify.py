from __future__ import annotations

import re
import json

import pytest
from tests.support.net_env import stub_verify_collection  # noqa: F401
from nika.net_env.dc_clos.lab import DCClos
from nika.net_env.dc_clos.verify import verify_dc_clos_lab, verify_dc_clos_lab_startup
from nika.net_env.enterprise_branch.verify import (
    verify_enterprise_branch_lab,
    verify_enterprise_branch_lab_startup,
)
from nika.net_env.k8s_lab.verify import verify_k8s_lab, verify_k8s_lab_startup
from nika.net_env.llmd_lab.verify import verify_llmd_lab, verify_llmd_lab_startup
from nika.net_env.net_env_pool import get_net_env_instance
from nika.net_env.sdn_l3_clos.topology_model import build_clos_fabric_model
from nika.net_env.sdn_l3_clos.verify import (
    verify_sdn_l3_clos_lab,
    verify_sdn_l3_clos_lab_startup,
)
from nika.runtime.factory import resolve_backend, runtime_for_session
from tests.support.integration_base import IntegrationTestCase
from tests.support.net_env import (
    assert_verify_success,
    instantiate_with_mocked_kathara,
)
from tests.support.prerequisites import docker_available
from tests.support.scenario_evaluate import evaluate_scenario
from tests.support.simple_bgp.verify import verify_simple_bgp_lab

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
    "onos",
    "fabric_mgr",
    "spine_1",
    "spine_2",
    "leaf_1",
    "leaf_2",
    "leaf_3",
    "leaf_4",
    "web_1",
    "web_2",
    "web_3",
    "web_4",
    "client_1_1",
    "client_2_1",
    "client_3_1",
    "client_4_1",
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
    "hq_corp_pc",
    "hq_srv",
    "hq_guest_pc",
    "hq_edge",
    "dc2_corp_pc",
    "dc2_srv",
    "dc2_guest_pc",
    "dc2_edge",
    "br1_corp_pc",
    "br1_guest_pc",
    "br1_edge",
    "br2_corp_pc",
    "br2_guest_pc",
    "br2_edge",
    "isp1_core",
    "internet_web",
    "isp2_core",
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
    "web_1": ("10.0.1.11",),
    "web_2": ("10.0.2.11",),
    "client_1_1": ("10.0.1.12",),
    "client_2_1": ("10.0.2.12",),
    "collector": ("10.0.0.3",),
    "onos": ("172.31.0.100",),
    "fabric_mgr": ("172.31.0.101",),
    "controller": ("201.1.1.2", "200.0.0.1"),
    "client": ("3.0.0.2", "200.0.0.7"),
    "hq_corp_pc": ("10.0.10.2",),
    "hq_srv": ("10.0.20.2",),
    "hq_guest_pc": ("10.0.40.2",),
    "dc2_corp_pc": ("10.10.10.2",),
    "dc2_srv": ("10.10.20.2",),
    "dc2_guest_pc": ("10.10.40.2",),
    "br1_corp_pc": ("10.1.10.2",),
    "br1_guest_pc": ("10.1.40.2",),
    "br2_corp_pc": ("10.2.10.2",),
    "br2_guest_pc": ("10.2.40.2",),
}


def _bgp_summary(
    established: list[str], idle: tuple[str, ...] = (), *, json_output=False
) -> str:
    """Render FRR ``show bgp summary`` with Established and Idle neighbors."""
    if json_output:
        return json.dumps(
            {
                "ipv4Unicast": {
                    "peers": {
                        **{
                            p: {"state": "Established", "hostname": "router"}
                            for p in established
                        },
                        **{p: {"state": "Idle"} for p in idle},
                    }
                }
            }
        )
    rows = [f"{peer} 4 65000 25 9 6 0 0 00:00:30 5 1 N/A" for peer in established]
    rows += [f"{peer} 4 65000 0 0 0 0 0 never Idle 0 N/A" for peer in idle]
    return (
        "BGP table version 16\n"
        "Neighbor V AS MsgRcvd MsgSent TblVer InQ OutQ Up/Down "
        "State/PfxRcd PfxSnt Desc\n" + "\n".join(rows) + "\n\n"
        f"Total number of neighbors {len(rows)}\n"
    )


def _dc_clos_lab(
    idle: tuple[str, str] | None = None,
) -> tuple[DCClos, dict[tuple[str, str], str]]:
    """Return a dc_clos s lab and BGP summaries with its planned sessions up.

    ``idle`` = (router, peer) leaves that one planned session Idle.
    """
    lab = instantiate_with_mocked_kathara(
        "nika.net_env.dc_clos.lab.Kathara.get_instance", lambda: DCClos("s")
    )
    summaries = {}
    for router, peers in lab.bgp_neighbors.items():
        down = tuple(p for p in peers if idle == (router, p))
        up = [p for p in peers if p not in down]
        summaries[router, "vtysh -c 'show bgp summary json'"] = _bgp_summary(
            up, down, json_output=True
        )
    return lab, summaries


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
        if command.startswith("dig +short"):
            return re.search(r"@(\S+)", command)[1]
        if command.startswith("ping -c 3 -i"):
            return "3 packets transmitted, 3 received, 0% packet loss"
        if command.startswith("sh -c "):
            # Batched probes: every ping, dig, and curl in the script succeeds.
            lines = [
                f"{target} 3 received"
                for target in re.findall(r"ping -c 3 -i 0\.2 -W 2 -q (\S+)", command)
            ]
            lines += [
                f"dns {addr} {addr}" for addr in re.findall(r"echo dns (\S+)", command)
            ]
            lines += [
                f"http {url} 200" for url in re.findall(r"echo http (\S+)", command)
            ]
            return "\n".join(lines)
        # Guest/IOT: isolation checks use count=1 and must fail.
        if command.startswith("ping -c") and ("guest" in host or "iot" in host):
            return "0 packets received"
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
        if command == "ip -d link show type vrf":
            return (
                "10: vrf_corp: <NOARP,MASTER,UP> ...\n"
                "11: vrf_server: <NOARP,MASTER,UP> ...\n"
                "12: vrf_guest: <NOARP,MASTER,UP> ...\n"
                "13: vrf_iot: <NOARP,MASTER,UP> ...\n"
            )
        if command == "systemctl is-active frr":
            return "active"
        if command == "pgrep -x bgpd":
            return "123"
        if command in {"systemctl is-active named", "systemctl is-active apache2"}:
            return "active"
        if command == "pgrep -x simple_switch":
            return "123"
        if command == "pgrep -x python3":
            return "456"
        if command == "pgrep -x java" or command.startswith("pgrep -af java"):
            return "789 java"
        if command.startswith("pgrep -af onos"):
            return "789 onos"
        if command == "ovs-vsctl show":
            return "Bridge br0"
        if "dump-flows" in command:
            return "cookie=0x1, priority=30000,ip,nw_dst=10.0.2.0/24 actions=group:4098"
        if "dump-groups" in command:
            return (
                "group_id=4098,type=select,selection_method=hash,"
                "bucket=actions=output:eth2,bucket=actions=output:eth3"
            )
        if "dump-ports" in command:
            return "OFPST_PORT reply"
        if "onos/v1/devices" in command:
            return (
                '{"devices":[{"id":"of:0000000000001001","available":true},'
                '{"id":"of:0000000000001002","available":true},'
                '{"id":"of:0000000000001003","available":true},'
                '{"id":"of:0000000000001004","available":true},'
                '{"id":"of:0000000000002001","available":true},'
                '{"id":"of:0000000000002002","available":true}]}'
            )
        if "onos/v1/links" in command:
            return '{"links":[{},{},{},{},{},{},{},{}]}'
        if "onos/v1/hosts" in command:
            return '{"hosts":[]}'
        if command.startswith("ip neigh show"):
            return "10.0.1.1 lladdr 02:00:00:00:00:01 REACHABLE"
        if command in {
            "vtysh -c 'show bgp summary'",
            "vtysh -c 'show bgp summary json'",
        }:
            # Established numbered peers (enterprise_branch /30 tunnels),
            # unnumbered k8s_lab fabric peers, and the k8s_lab AS1-AS2 link.
            peers = [f"172.30.0.{i}" for i in range(1, 40)]
            peers += [f"peer({iface})" for iface in ("eth0", "eth1", "eth2", "eth3")]
            peers += ["10.0.4.1", "10.0.4.2"]
            return _bgp_summary(peers, json_output="json" in command)
        if command == "vtysh -c 'show ip bgp'":
            return (
                "*> 10.0.10.0/24\n*> 10.0.20.0/24\n"
                "*> 10.10.10.0/24\n*> 10.10.20.0/24\n"
                "*> 10.1.10.0/24\n*> 10.2.10.0/24\n"
            )
        if "show ip route vrf vrf_corp" in command:
            return (
                "C>* 10.1.10.0/24 is directly connected\n"
                "B>* 10.0.10.0/24 [20/0] via 172.30.0.1\n"
                "B>* 10.0.20.0/24 [20/0] via 172.30.0.1\n"
                "B>* 10.10.10.0/24 [20/0] via 172.30.0.1\n"
                "B>* 10.10.20.0/24 [20/0] via 172.30.0.1\n"
                "B>* 10.2.10.0/24 [20/0] via 172.30.0.1\n"
            )
        if command.startswith("ip route show vrf vrf_guest") or command.startswith(
            "ip route show vrf vrf_iot"
        ):
            return "default via 100.64.0.2\n"
        if command == "vtysh -c 'show ip route'":
            return "C>* 100.64.0.0/30 is directly connected\n"
        if command.startswith("vtysh -c 'show ip bgp 10."):
            # Include several /30 hub tunnel IPs so primary-path checks pass.
            return (
                "*  172.30.0.1\n*> 172.30.0.1 from 172.30.0.1\n"
                "*  172.30.0.9\n*> 172.30.0.9 from 172.30.0.9\n"
                "*  172.30.0.21\n*> 172.30.0.21 from 172.30.0.21\n"
            )
        if command.startswith("wg show"):
            return "interface: wg0\n  listening port: 51820\n"
        if command.startswith("tc qdisc show dev"):
            return (
                "qdisc htb 1: root refcnt 2 r2q 10 default 0x20\n"
                "qdisc pfifo 10: parent 1:10\n"
                "qdisc bfifo 20: parent 1:20\n"
            )
        if command.startswith("tc class show dev"):
            return (
                "class htb 1:1 root rate 8Mbit ceil 8Mbit\n"
                "class htb 1:10 parent 1:1 prio 1 rate 2Mbit ceil 8Mbit\n"
                "class htb 1:20 parent 1:1 prio 2 rate 6Mbit ceil 6Mbit\n"
            )
        if command.startswith("ip route get"):
            return "10.2.10.2 via 172.30.0.1 dev wg_hq src 10.1.10.1"
        if command == "ip route":
            return "100.64.0.0/30 dev eth0\n100.64.0.4/30 dev eth1\n"
        if command.startswith("curl -s -o /dev/null"):
            return "200"
        if command == "kubectl get nodes --no-headers":
            return "\n".join(
                (f"node{idx} Ready control-plane 1m v1.0" for idx in range(6))
            )
        if command == "kubectl get ns word-ns -o jsonpath={.status.phase}":
            return "Active"
        if command == "kubectl get ns weather-ns -o jsonpath={.status.phase}":
            return "Active"
        if command == "kubectl get ns llm-d -o jsonpath={.status.phase}":
            return "Active"
        if "jsonpath={.status.loadBalancer.ingress[0].ip}" in command:
            return "101.0.0.1"
        if command == "kubectl get pods -n metallb-system --no-headers":
            return "speaker-x 1/1 Running 0 1m\ncontroller-y 1/1 Running 0 1m"
        if command.startswith("kubectl get gateway -n llm-d llm-d-gateway"):
            return "200.0.0.240"
        if command == "kubectl get pods -n agentgateway-system --no-headers":
            return "agentgateway-x 1/1 Running 0 1m"
        if command == "kubectl get pods -n llm-d --no-headers":
            return "gaie-epp-x 1/1 Running 0 1m\nms-decode-y 2/2 Running 0 1m"
        if command == "kubectl get gateway -A --no-headers":
            return "default pd-gateway"
        return ""


@pytest.mark.unit
@pytest.mark.usefixtures("stub_verify_collection")
class KatharaVerifyUnitTest:
    def assert_verified(self, result: dict) -> None:
        assert_verify_success(result)

    def test_simple_bgp_verify_passes(self) -> None:
        assert_verify_success(verify_simple_bgp_lab(FakeRuntime(), scenario_name="x"))

    def test_dc_clos_startup_verify_passes(self) -> None:
        lab, summaries = _dc_clos_lab()
        assert_verify_success(
            verify_dc_clos_lab_startup(
                FakeRuntime(overrides=summaries),
                scenario_name="dc_clos",
                nodes=tuple(lab.lab.machines),
                bgp_neighbors=lab.bgp_neighbors,
                client_addresses=lab.client_addresses,
            )
        )

    def test_dc_clos_verify_passes(self) -> None:
        lab, summaries = _dc_clos_lab()
        assert_verify_success(
            verify_dc_clos_lab(
                FakeRuntime(overrides=summaries),
                scenario_name="dc_clos",
                nodes=tuple(lab.lab.machines),
                bgp_neighbors=lab.bgp_neighbors,
                client_addresses=lab.client_addresses,
                dns_servers=lab.dns_servers,
                web_urls=tuple(lab.web_urls),
            )
        )

    def test_dc_clos_startup_one_idle_session_fails(self) -> None:
        # "Total number of neighbors N" and "BGP table version N" must not
        # count as Established sessions.
        lab, summaries = _dc_clos_lab(idle=("leaf_router_0_1", "172.16.0.6"))
        assert "172.16.0.6" in lab.bgp_neighbors["leaf_router_0_1"]
        result = verify_dc_clos_lab_startup(
            FakeRuntime(overrides=summaries),
            scenario_name="dc_clos",
            nodes=tuple(lab.lab.machines),
            bgp_neighbors=lab.bgp_neighbors,
            client_addresses=lab.client_addresses,
        )
        assert not result["verified"]
        assert not result["checks"]["bgp_sessions_established"]
        assert result["details"]["failures"]["bgp_not_established"] == {
            "leaf_router_0_1": ["172.16.0.6"]
        }

    def test_sdn_l3_clos_startup_verify_passes(self) -> None:
        model = build_clos_fabric_model("s")
        assert_verify_success(
            verify_sdn_l3_clos_lab_startup(
                FakeRuntime(), scenario_name="sdn_l3_clos", model=model
            )
        )

    def test_k8s_startup_verify_passes(self) -> None:
        assert_verify_success(verify_k8s_lab_startup(FakeRuntime(), scenario_name="x"))

    def test_llmd_startup_verify_passes(self) -> None:
        assert_verify_success(verify_llmd_lab_startup(FakeRuntime(), scenario_name="x"))

    def test_enterprise_branch_startup_verify_passes(self) -> None:
        from nika.net_env.enterprise_branch.topology import build_topo_spec

        spec = build_topo_spec("s")
        assert_verify_success(
            verify_enterprise_branch_lab_startup(
                FakeRuntime(),
                scenario_name="enterprise_branch",
                topo_size="s",
                spec=spec,
            )
        )

    def test_enterprise_branch_startup_frr_failure(self) -> None:
        from nika.net_env.enterprise_branch.topology import build_topo_spec

        spec = build_topo_spec("s")
        result = verify_enterprise_branch_lab_startup(
            FakeRuntime(
                overrides={("hq_edge", "pgrep -x bgpd"): ""},
            ),
            scenario_name="enterprise_branch",
            topo_size="s",
            spec=spec,
        )
        assert not result["verified"]
        assert not result["checks"]["hq_edge_frr"]

    def test_enterprise_branch_verify_passes(self) -> None:
        from nika.net_env.enterprise_branch.topology import (
            BuiltTunnel,
            build_topo_spec,
        )

        # Mirror the scale-s tunnel graph with sequential /30 tunnel IPs so BGP
        # peer checks and primary-path preference line up with FakeRuntime.
        spec = build_topo_spec("s")
        tunnels: list[BuiltTunnel] = []
        tun_i = 0
        for tspec in spec.tunnels:
            hub_ip = f"172.30.0.{tun_i * 4 + 1}"
            spoke_ip = f"172.30.0.{tun_i * 4 + 2}"
            tun_i += 1
            hub_iface = (
                f"wg_{tspec.local_site}"
                if tspec.primary
                else f"wg_{tspec.local_site}_b"
            )
            tunnels.append(
                BuiltTunnel(
                    spoke=tspec.local_site,
                    hub=tspec.remote_site,
                    provider=tspec.provider,
                    primary=tspec.primary,
                    local_pref=tspec.local_pref,
                    spoke_iface=tspec.iface,
                    hub_iface=hub_iface,
                    spoke_tunnel_ip=spoke_ip,
                    hub_tunnel_ip=hub_ip,
                    spoke_wan_ip="100.64.0.5",
                    hub_wan_ip="100.64.0.1",
                    listen_port_spoke=tspec.listen_port,
                    listen_port_hub=51820 + tun_i,
                )
            )
        assert_verify_success(
            verify_enterprise_branch_lab(
                FakeRuntime(),
                scenario_name="enterprise_branch",
                topo_size="s",
                built_tunnels=tunnels,
                spec=spec,
            )
        )

    def test_sdn_l3_clos_verify_passes(self) -> None:
        model = build_clos_fabric_model("s")
        assert_verify_success(
            verify_sdn_l3_clos_lab(
                FakeRuntime(), scenario_name="sdn_l3_clos", model=model
            )
        )

    def test_k8s_verify_passes(self) -> None:
        assert_verify_success(verify_k8s_lab(FakeRuntime(), scenario_name="x"))

    def test_llmd_verify_passes(self) -> None:
        assert_verify_success(verify_llmd_lab(FakeRuntime(), scenario_name="x"))

    def test_llmd_unready_model_server_fails(self) -> None:
        result = verify_llmd_lab(
            FakeRuntime(
                overrides={
                    (
                        "controller",
                        "kubectl get pods -n llm-d --no-headers",
                    ): "gaie-epp-x 1/1 Running 0 1m\n"
                    "ms-decode-y 1/2 CrashLoopBackOff 5 9m"
                }
            ),
            scenario_name="x",
        )
        assert not result["verified"]
        assert not result["checks"]["serving_pods_ready"]
        assert result["details"]["failures"]["unready_pods"] == {
            "llm-d": ["ms-decode-y"]
        }

    def test_missing_node_fails(self) -> None:
        result = verify_simple_bgp_lab(
            FakeRuntime(nodes=ALL_NODES - {"pc2"}), scenario_name="x"
        )

        assert not result["verified"]

        assert not result["checks"]["nodes_deployed"]

    def test_k8s_startup_not_ready_fails(self) -> None:
        result = verify_k8s_lab_startup(
            FakeRuntime(
                overrides={("controller", "kubectl get nodes --no-headers"): ""}
            ),
            scenario_name="x",
        )
        assert not result["verified"]
        assert not result["checks"]["k3s_nodes_ready"]

    def test_k8s_not_ready_fails(self) -> None:
        result = verify_k8s_lab(
            FakeRuntime(
                overrides={("controller", "kubectl get nodes --no-headers"): ""}
            ),
            scenario_name="x",
        )

        assert not result["verified"]

        assert not result["checks"]["k3s_nodes_ready"]


@pytest.mark.integration
@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class KatharaScenarioVerifyIntegrationTest(IntegrationTestCase):
    def test_simple_bgp_starts_and_verifies(self) -> None:
        scenario = "simple_bgp"
        session_id = self._start_env(scenario, [])
        try:
            row = self._assert_session_ready(session_id, scenario)
            assert resolve_backend(row) == "kathara"
            nodes = set(runtime_for_session(row).list_nodes())
            assert {"router1", "router2", "pc1", "pc2"} <= nodes

            kwargs = self._scenario_kwargs(session_id)
            kwargs["backend"] = resolve_backend(row)
            net_env = get_net_env_instance(scenario, **kwargs)
            ok, result = evaluate_scenario(net_env)
            assert ok is True, result
        finally:
            self._close_session(session_id)


@pytest.mark.unit
def test_bgp_summary_json_preserves_peer_ip_when_hostnames_are_enabled():
    import json

    from nika.net_env.verify import frr_bgp_established_peers

    summary = json.dumps(
        {
            "ipv4Unicast": {
                "peers": {
                    "10.255.0.2": {"hostname": "ucla", "state": "Established"},
                    "10.255.0.3": {"hostname": "uscb", "state": "Idle"},
                }
            }
        }
    )
    assert frr_bgp_established_peers(summary) == {"10.255.0.2"}
