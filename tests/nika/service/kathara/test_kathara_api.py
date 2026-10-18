from __future__ import annotations

import pytest
from nika.runtime.factory import resolve_backend
from nika.service.containerlab import create_host_api
from nika.service.kathara import KatharaBaseAPI
from tests.support.prerequisites import docker_available
from tests.support.api_smoke import assert_json_payload
from tests.support.kathara_api_base import KatharaScenarioApiSmokeTest

pytestmark = pytest.mark.integration

HOST = "pc1"
HOST2 = "pc2"
ROUTER = "router1"
ROUTER2 = "router2"
INTF = "eth0"
FRR_CONF = "/etc/frr/frr.conf"
EXPECTED_NODES = frozenset({"pc1", "pc2", "router1", "router2"})


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class KatharaApiSmokeTest(KatharaScenarioApiSmokeTest):
    SCENARIO = "simple_bgp"

    def _peer_ip(self, host: str = HOST2) -> str:
        ip = self._host_api().get_host_ip(host, INTF)
        assert ip is not None
        return str(ip)

    def test_session_backend(self) -> None:
        row = self._session_row(self.session_id)
        assert resolve_backend(row) == "kathara"

    def test_runtime_lifecycle_apis(self) -> None:
        runtime = self._runtime()
        nodes = self.smoke("runtime.list_nodes", runtime.list_nodes, expect_type=list)
        assert EXPECTED_NODES.issubset(set(nodes))
        self.smoke("runtime.exists", runtime.exists, expect_type=bool)
        inspect = self.smoke("runtime.inspect", runtime.inspect, expect_type=list)
        assert len(inspect) > 0
        container = self.smoke(
            "runtime.get_container", lambda: runtime.get_container(HOST)
        )
        assert container is not None
        status = self.smoke(
            "runtime.node_status",
            lambda: runtime.node_status(HOST),
            expect_type=str,
            min_len=1,
        )
        assert status in {"running", "paused", "not_found"}

    def test_runtime_semantic_host_apis(self) -> None:
        runtime = self._runtime()
        self.smoke(
            "runtime.get_host_ip",
            lambda: runtime.get_host_ip(HOST, INTF),
            expect_type=str,
            min_len=7,
        )
        self.smoke(
            "runtime.get_default_gateway",
            lambda: runtime.get_default_gateway(HOST),
            expect_type=str,
            min_len=7,
        )
        ifaces = self.smoke(
            "runtime.get_host_interfaces",
            lambda: runtime.get_host_interfaces(HOST),
            expect_type=list,
        )
        assert INTF in ifaces
        self.smoke(
            "runtime.get_host_mac_address",
            lambda: runtime.get_host_mac_address(HOST, INTF),
            expect_type=str,
            min_len=11,
        )
        operstate = self.smoke(
            "runtime.get_interface_operstate",
            lambda: runtime.get_interface_operstate(HOST, INTF),
            expect_type=str,
            min_len=2,
        )
        assert operstate in {"up", "down", "unknown"}
        assert self.smoke(
            "runtime.interface_exists",
            lambda: runtime.interface_exists(HOST, INTF),
            expect_type=bool,
        )
        self.smoke(
            "runtime.tc_show_intf", lambda: runtime.tc_show_intf(HOST, INTF), min_len=1
        )
        self.smoke(
            "runtime.list_nft_ruleset",
            lambda: runtime.list_nft_ruleset(HOST),
            min_len=1,
        )
        peers = self.smoke(
            "runtime.get_connected_devices",
            lambda: runtime.get_connected_devices(HOST),
            expect_type=list,
        )
        assert peers
        assert self.smoke(
            "runtime.ping_ok",
            lambda: runtime.ping_ok(HOST, self._peer_ip()),
            expect_type=bool,
        )

    def test_runtime_semantic_process_file_and_network_checks(self) -> None:
        runtime = self._runtime()
        dhcp_clients = self.smoke(
            "runtime.list_dhcp_client_nodes",
            runtime.list_dhcp_client_nodes,
            expect_type=list,
        )
        # simple_bgp hosts are statically addressed and declare no dhcp_client role.
        assert dhcp_clients == []
        assert self.smoke(
            "runtime.process_running(zebra)",
            lambda: runtime.process_running(ROUTER, "zebra"),
            expect_type=bool,
        )
        assert self.smoke(
            "runtime.process_not_running(fake)",
            lambda: runtime.process_not_running(HOST, "nonexistent_proc_xyz"),
            expect_type=bool,
        )
        assert self.smoke(
            "runtime.file_contains(frr.conf)",
            lambda: runtime.file_contains(ROUTER, FRR_CONF, "router bgp"),
            expect_type=bool,
        )
        self.smoke(
            "runtime.iptables_rule_present",
            lambda: runtime.iptables_rule_present(HOST, "INPUT", "-j ACCEPT"),
            expect_type=bool,
        )
        self.smoke(
            "runtime.nft_ruleset_contains",
            lambda: runtime.nft_ruleset_contains(HOST, "table"),
            expect_type=bool,
        )
        self.smoke(
            "runtime.tc_qdisc_contains",
            lambda: runtime.tc_qdisc_contains(HOST, INTF, "qdisc"),
            expect_type=bool,
        )
        self.smoke(
            "runtime.dig_query",
            lambda: runtime.dig_query(HOST, "example.com"),
            expect_type=str,
        )
        assert not self.smoke(
            "runtime.uses_srl_router",
            lambda: runtime.uses_srl_router(ROUTER),
            expect_type=bool,
        )

    def test_runtime_frr_api(self) -> None:
        runtime = self._runtime()
        asn = self.smoke(
            "runtime.frr_get_bgp_asn_number",
            lambda: runtime.frr_get_bgp_asn_number(ROUTER),
            expect_type=int,
        )
        assert asn > 0

    def test_runtime_lab_api_adapter(self) -> None:
        runtime = self._runtime()
        lab_api = self.smoke("runtime.lab_api", lambda: runtime.lab_api)
        from nika.service.lab.adapters import LabRuntimeLabAPI

        assert isinstance(lab_api, LabRuntimeLabAPI)
        self.smoke(
            "lab_api.get_host_ip",
            lambda: lab_api.get_host_ip(HOST, INTF),
            expect_type=str,
            min_len=7,
        )
        self.smoke(
            "lab_api.frr_get_bgp_asn_number",
            lambda: lab_api.frr_get_bgp_asn_number(ROUTER),
            expect_type=int,
        )
        self.smoke(
            "lab_api.tc_show_statistics",
            lambda: lab_api.tc_show_statistics(HOST, INTF),
            min_len=1,
        )
        self.smoke(
            "lab_api.intf_show", lambda: lab_api.intf_show(HOST, INTF), min_len=1
        )

    def test_kathara_host_api(self) -> None:
        api = self._host_api()
        self.smoke(
            "KatharaBaseAPI.exec_cmd", lambda: api.exec_cmd(HOST, "hostname"), min_len=1
        )
        cfg = self.smoke(
            "KatharaBaseAPI.get_host_net_config",
            lambda: api.get_host_net_config(HOST),
            expect_type=dict,
        )
        assert cfg["host_name"] == HOST
        assert "ip_route" in cfg
        self.smoke(
            "KatharaBaseAPI.ping_pair",
            lambda: api.ping_pair(HOST, HOST2, count=1),
            min_len=1,
        )
        self.smoke(
            "KatharaBaseAPI.get_host_ip",
            lambda: api.get_host_ip(HOST, INTF),
            expect_type=str,
            min_len=7,
        )
        self.smoke(
            "KatharaBaseAPI.get_default_gateway",
            lambda: api.get_default_gateway(HOST),
            expect_type=str,
            min_len=7,
        )
        self.smoke(
            "KatharaBaseAPI.get_host_mac_address",
            lambda: api.get_host_mac_address(HOST, INTF),
            expect_type=str,
            min_len=11,
        )
        ifaces = self.smoke(
            "KatharaBaseAPI.get_host_interfaces",
            lambda: api.get_host_interfaces(HOST),
            expect_type=list,
        )
        assert INTF in ifaces
        peers = self.smoke(
            "KatharaBaseAPI.get_connected_devices",
            lambda: api.get_connected_devices(HOST),
            expect_type=list,
        )
        assert peers
        links = self.smoke(
            "KatharaBaseAPI.get_links", lambda: api.get_links(), expect_type=dict
        )
        assert links
        self.smoke("KatharaBaseAPI.netstat", lambda: api.netstat(HOST), min_len=1)
        self.smoke(
            "KatharaBaseAPI.ip_addr_statistics",
            lambda: api.ip_addr_statistics(HOST),
            min_len=1,
        )
        self.smoke("KatharaBaseAPI.ethtool", lambda: api.ethtool(HOST, INTF), min_len=1)
        self.smoke("KatharaBaseAPI.ps", lambda: api.ps(HOST), min_len=1)
        hosts = self.smoke("KatharaBaseAPI.get_hosts", api.get_hosts, expect_type=list)
        assert HOST in hosts
        assert HOST2 in hosts
        base_hosts = self.smoke(
            "KatharaBaseAPI.get_base_hosts", api.get_base_hosts, expect_type=list
        )
        assert HOST in base_hosts
        self.smoke("KatharaBaseAPI.load_machines", api.load_machines)
        assert HOST in api.hosts
        assert ROUTER in api.routers
        self.smoke(
            "KatharaBaseAPI.traceroute",
            lambda: api.traceroute(HOST, self._peer_ip()),
            min_len=1,
        )
        self.smoke(
            "KatharaBaseAPI.systemctl_ops(status)",
            lambda: api.systemctl_ops(ROUTER, "frr", "status"),
            min_len=1,
        )
        self.smoke(
            "KatharaBaseAPI.show_dns_config",
            lambda: api.show_dns_config(HOST),
            expect_type=str,
        )
        gateway = api.get_default_gateway(HOST)
        assert gateway is not None
        self.smoke(
            "KatharaBaseAPI.curl_web_test",
            lambda: api.curl_web_test(HOST, f"http://{gateway}", times=1),
            min_len=1,
        )

        async def _exec_async() -> str:
            return await api.exec_cmd_async(HOST, "hostname")

        self.smoke_async("KatharaBaseAPI.exec_cmd_async", _exec_async, min_len=1)

    def test_kathara_reachability_json(self) -> None:
        api = self._host_api()

        async def _reachability() -> str:
            return await api.get_reachability()

        payload = self.smoke_async(
            "KatharaBaseAPI.get_reachability", _reachability, min_len=2
        )
        parsed = assert_json_payload("KatharaBaseAPI.get_reachability", payload)
        assert "results" in parsed
        assert parsed["results"]

    def test_kathara_frr_api(self) -> None:
        api = self._frr_api()
        self.smoke(
            "KatharaFRRAPI.frr_get_bgp_conf",
            lambda: api.frr_get_bgp_conf(ROUTER),
            min_len=1,
        )
        self.smoke(
            "KatharaFRRAPI.frr_show_running_config",
            lambda: api.frr_show_running_config(ROUTER),
            min_len=1,
        )
        self.smoke(
            "KatharaFRRAPI.frr_show_route",
            lambda: api.frr_show_route(ROUTER),
            min_len=1,
        )
        asn = self.smoke(
            "KatharaFRRAPI.frr_get_bgp_asn_number",
            lambda: api.frr_get_bgp_asn_number(ROUTER),
            expect_type=int,
        )
        assert asn > 0

    def test_kathara_intf_api(self) -> None:
        api = self._intf_api()
        self.smoke(
            "KatharaIntfAPI.intf_show", lambda: api.intf_show(HOST, INTF), min_len=1
        )

    def test_kathara_tc_api(self) -> None:
        api = self._tc_api()
        self.smoke(
            "KatharaTCAPI.tc_show_intf", lambda: api.tc_show_intf(HOST, INTF), min_len=1
        )
        self.smoke(
            "KatharaTCAPI.tc_show_statistics",
            lambda: api.tc_show_statistics(HOST, INTF),
            min_len=1,
        )
        self.smoke(
            "KatharaTCAPI.tc_qdisc_contains",
            lambda: api.tc_qdisc_contains(HOST, INTF, "qdisc"),
            expect_type=bool,
        )

    def test_kathara_nft_api(self) -> None:
        api = self._nft_api()
        self.smoke(
            "KatharaNFTableAPI.list_nft_ruleset",
            lambda: api.list_nft_ruleset(HOST),
            min_len=1,
        )
        self.smoke(
            "KatharaNFTableAPI.nft_list_ruleset",
            lambda: api.nft_list_ruleset(HOST),
            min_len=1,
        )
        self.smoke(
            "KatharaNFTableAPI.nft_list_tables",
            lambda: api.nft_list_tables(HOST),
            min_len=1,
        )
        self.smoke(
            "KatharaNFTableAPI.nft_list_chains",
            lambda: api.nft_list_chains(HOST),
            min_len=1,
        )
        self.smoke(
            "KatharaNFTableAPI.nft_ruleset_contains",
            lambda: api.nft_ruleset_contains(HOST, "table"),
            expect_type=bool,
        )

    def test_create_host_api_factory(self) -> None:
        api = self.smoke(
            "create_host_api(kathara)",
            lambda: create_host_api(lab_name=self._lab_name(), backend="kathara"),
        )
        assert isinstance(api, KatharaBaseAPI)
        self.smoke(
            "create_host_api.get_host_ip",
            lambda: api.get_host_ip(HOST, INTF),
            expect_type=str,
            min_len=7,
        )
