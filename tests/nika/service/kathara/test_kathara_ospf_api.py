from __future__ import annotations

import pytest
from typing import ClassVar
from nika.net_env.kathara.intradomain_routing.campus_lan.verify import (
    CORE_ROUTER,
    DNS_SERVER,
    PROBE_HOST,
    WEB0_URL,
    WEB3_URL,
)
from tests.support.prerequisites import docker_available
from tests.support.kathara_api_base import KatharaScenarioApiSmokeTest

HOST = PROBE_HOST
ROUTER = CORE_ROUTER
INTF = "eth0"


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class KatharaOspfApiSmokeTest(KatharaScenarioApiSmokeTest):
    SCENARIO = "campus_lan"
    ENV_RUN_ARGS: ClassVar[list[str]] = ["-s", "s", "--workload", "static"]

    def test_runtime_ospf_semantic_apis(self) -> None:
        runtime = self._runtime()

        assert self.smoke(
            "runtime.process_running(ospfd)",
            lambda: runtime.process_running(ROUTER, "ospfd"),
            expect_type=bool,
        )
        self.smoke(
            "runtime.dig_query(web0.local)",
            lambda: runtime.dig_query(HOST, "web0.local"),
            min_len=1,
        )

    def test_kathara_frr_ospf_api(self) -> None:
        api = self._frr_api()
        self.smoke(
            "KatharaFRRAPI.frr_get_ospf_conf",
            lambda: api.frr_get_ospf_conf(ROUTER),
            min_len=1,
        )
        neighbors = self.smoke(
            "KatharaFRRAPI.frr_get_ospf_neighbors",
            lambda: api.frr_get_ospf_neighbors(ROUTER),
            min_len=1,
        )

        assert "Full" in neighbors
        self.smoke(
            "KatharaFRRAPI.frr_get_ospf_routes",
            lambda: api.frr_get_ospf_routes(ROUTER),
            min_len=1,
        )
        self.smoke(
            "KatharaFRRAPI.frr_get_ospf_interfaces",
            lambda: api.frr_get_ospf_interfaces(ROUTER),
            min_len=1,
        )
        self.smoke(
            "KatharaFRRAPI.frr_exec(show ip ospf)",
            lambda: api.frr_exec(ROUTER, "show ip ospf"),
            min_len=1,
        )
        self.smoke(
            "KatharaFRRAPI.frr_show_route",
            lambda: api.frr_show_route(ROUTER),
            min_len=1,
        )

    def test_kathara_host_dns_and_web_api(self) -> None:
        api = self._host_api()
        dns_cfg = self.smoke(
            "KatharaBaseAPI.show_dns_config",
            lambda: api.show_dns_config(HOST),
            min_len=1,
        )

        assert "nameserver" in dns_cfg.lower()
        self.smoke(
            "KatharaBaseAPI.curl_web_test(web0)",
            lambda: api.curl_web_test(HOST, WEB0_URL, times=1),
            min_len=1,
        )
        self.smoke(
            "KatharaBaseAPI.curl_web_test(web3)",
            lambda: api.curl_web_test(HOST, WEB3_URL, times=1),
            min_len=1,
        )
        self.smoke(
            "KatharaBaseAPI.systemctl_ops(named)",
            lambda: api.systemctl_ops(DNS_SERVER, "named", "status"),
            min_len=1,
        )
        hosts = self.smoke("KatharaBaseAPI.get_hosts", api.get_hosts, expect_type=list)

        assert HOST in hosts
