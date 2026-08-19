from __future__ import annotations

import pytest
from typing import ClassVar
from nika.net_env.kathara.intradomain_routing.campus_lan.verify import (
    DNS_SERVER,
    PROBE_HOST,
    WEB0_URL,
)
from nika.runtime.factory import resolve_backend
from tests.support.prerequisites import docker_available
from tests.support.kathara_api_base import KatharaScenarioApiSmokeTest

HOST = PROBE_HOST
INTF = "eth0"
DHCP_SERVER = "dhcp_server"


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class KatharaDhcpApiSmokeTest(KatharaScenarioApiSmokeTest):
    SCENARIO = "campus_lan"
    ENV_RUN_ARGS: ClassVar[list[str]] = ["-s", "s", "--workload", "dhcp"]

    def test_session_backend(self) -> None:
        row = self._session_row(self.session_id)

        assert resolve_backend(row) == "kathara"

    def test_runtime_dhcp_semantic_apis(self) -> None:
        runtime = self._runtime()
        clients = self.smoke(
            "runtime.list_dhcp_client_nodes",
            runtime.list_dhcp_client_nodes,
            expect_type=list,
        )

        assert HOST in clients
        self.smoke(
            "runtime.get_host_ip(dhcp client)",
            lambda: runtime.get_host_ip(HOST, INTF),
            expect_type=str,
            min_len=7,
        )
        self.smoke(
            "runtime.dig_query(web0.local)",
            lambda: runtime.dig_query(HOST, "web0.local"),
            min_len=1,
        )

        assert self.smoke(
            "runtime.file_contains(dhcpd.conf)",
            lambda: runtime.file_contains(
                DHCP_SERVER, "/etc/dhcp/dhcpd.conf", "subnet"
            ),
            expect_type=bool,
        )

    def test_kathara_host_dhcp_dns_api(self) -> None:
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
            "KatharaBaseAPI.systemctl_ops(isc-dhcp-server)",
            lambda: api.systemctl_ops(DHCP_SERVER, "isc-dhcp-server", "status"),
            min_len=1,
        )
        self.smoke(
            "KatharaBaseAPI.systemctl_ops(named)",
            lambda: api.systemctl_ops(DNS_SERVER, "named", "status"),
            min_len=1,
        )
