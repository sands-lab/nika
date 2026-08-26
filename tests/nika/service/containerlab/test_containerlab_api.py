from __future__ import annotations

import pytest
from typing import ClassVar
from nika.net_env.min3clos.verify import (
    CLIENT1,
    CLIENT2,
    EXPECTED_NODES,
    LEAF1,
)
from nika.runtime.factory import resolve_backend, runtime_for_session
from nika.service.containerlab import (
    ContainerlabBaseAPI,
    ContainerlabSRLAPI,
    create_host_api,
)
from tests.support.api_smoke import ApiSmokeMixin, assert_json_payload
from tests.support.prerequisites import min3clos_prerequisites
from tests.support.integration_base import SharedSessionTestCase

CLIENT_INTF = "eth1"
LEAF_INTF = "e1-1"


@pytest.mark.skipif(
    not min3clos_prerequisites(), reason="containerlab, gnmic, or Docker not available"
)
class ContainerlabApiSmokeTest(SharedSessionTestCase, ApiSmokeMixin):
    SCENARIO = "min3clos"
    ENV_RUN_ARGS: ClassVar[list[str]] = []

    def _runtime(self):
        return runtime_for_session(self._session_row(self.session_id))

    def _host_api(self) -> ContainerlabBaseAPI:
        return create_host_api(
            lab_name=self._session_row(self.session_id)["lab_name"],
            backend="containerlab",
            session_meta=self._session_row(self.session_id),
        )

    def _srl_api(self) -> ContainerlabSRLAPI:
        host_api = self._host_api()
        return ContainerlabSRLAPI(host_api.runtime)

    def test_session_backend(self) -> None:
        row = self._session_row(self.session_id)
        assert resolve_backend(row) == "containerlab"
        assert row.get("topology_file") is not None

    def test_runtime_lifecycle_apis(self) -> None:
        runtime = self._runtime()
        nodes = self.smoke("runtime.list_nodes", runtime.list_nodes, expect_type=list)
        for name in EXPECTED_NODES:
            assert name in nodes
        self.smoke("runtime.exists", runtime.exists, expect_type=bool)
        inspect = self.smoke("runtime.inspect", runtime.inspect, expect_type=list)
        assert len(inspect) > 0
        container = self.smoke(
            "runtime.get_container", lambda: runtime.get_container(CLIENT1)
        )
        assert container is not None
        status = self.smoke(
            "runtime.node_status",
            lambda: runtime.node_status(CLIENT1),
            expect_type=str,
            min_len=1,
        )
        assert status in {"running", "paused", "not_found"}

    def test_runtime_semantic_host_apis(self) -> None:
        runtime = self._runtime()
        self.smoke(
            "runtime.get_host_ip",
            lambda: runtime.get_host_ip(CLIENT1, CLIENT_INTF),
            expect_type=str,
            min_len=7,
        )
        self.smoke(
            "runtime.get_default_gateway",
            lambda: runtime.get_default_gateway(CLIENT1),
            expect_type=str,
            min_len=7,
        )
        ifaces = self.smoke(
            "runtime.get_host_interfaces",
            lambda: runtime.get_host_interfaces(CLIENT1),
            expect_type=list,
        )
        assert CLIENT_INTF in ifaces
        self.smoke(
            "runtime.get_host_mac_address",
            lambda: runtime.get_host_mac_address(CLIENT1, CLIENT_INTF),
            expect_type=str,
            min_len=11,
        )
        operstate = self.smoke(
            "runtime.get_interface_operstate",
            lambda: runtime.get_interface_operstate(CLIENT1, CLIENT_INTF),
            expect_type=str,
            min_len=2,
        )
        assert operstate in {"up", "down", "unknown"}
        assert self.smoke(
            "runtime.interface_exists",
            lambda: runtime.interface_exists(CLIENT1, CLIENT_INTF),
            expect_type=bool,
        )
        self.smoke(
            "runtime.tc_show_intf",
            lambda: runtime.tc_show_intf(CLIENT1, CLIENT_INTF),
            min_len=1,
        )
        self.smoke(
            "runtime.list_nft_ruleset",
            lambda: runtime.list_nft_ruleset(CLIENT1),
            min_len=1,
        )
        peers = self.smoke(
            "runtime.get_connected_devices",
            lambda: runtime.get_connected_devices(CLIENT1),
            expect_type=list,
        )
        assert LEAF1 in peers
        assert self.smoke(
            "runtime.ping_ok",
            lambda: runtime.ping_ok(CLIENT1, runtime.get_host_ip(CLIENT2) or ""),
            expect_type=bool,
        )

    def test_runtime_srl_semantic_apis(self) -> None:
        runtime = self._runtime()
        assert self.smoke(
            "runtime.uses_srl_router(leaf1)",
            lambda: runtime.uses_srl_router(LEAF1),
            expect_type=bool,
        )
        assert not self.smoke(
            "runtime.uses_srl_router(client1)",
            lambda: runtime.uses_srl_router(CLIENT1),
            expect_type=bool,
        )
        asn = self.smoke(
            "runtime.srl_get_bgp_as",
            lambda: runtime.srl_get_bgp_as(LEAF1),
            expect_type=int,
        )
        assert asn > 0
        self.smoke(
            "runtime.get_interface_operstate(leaf)",
            lambda: runtime.get_interface_operstate(LEAF1, LEAF_INTF),
            expect_type=str,
            min_len=2,
        )

    def test_runtime_lab_api_adapter(self) -> None:
        runtime = self._runtime()
        lab_api = self.smoke("runtime.lab_api", lambda: runtime.lab_api)
        from nika.service.containerlab.adapters import LabRuntimeContainerlabAPI

        assert isinstance(lab_api, LabRuntimeContainerlabAPI)
        self.smoke(
            "lab_api.srl_get_bgp_as",
            lambda: lab_api.srl_get_bgp_as(LEAF1),
            expect_type=int,
        )

    def test_containerlab_host_api(self) -> None:
        api = self._host_api()
        self.smoke(
            "ContainerlabBaseAPI.exec_cmd",
            lambda: api.exec_cmd(CLIENT1, "hostname"),
            min_len=1,
        )
        cfg = self.smoke(
            "ContainerlabBaseAPI.get_host_net_config",
            lambda: api.get_host_net_config(CLIENT1),
            expect_type=dict,
        )
        assert cfg["host_name"] == CLIENT1
        assert "ip_route" in cfg
        self.smoke(
            "ContainerlabBaseAPI.ping_pair",
            lambda: api.ping_pair(CLIENT1, CLIENT2, count=1),
            min_len=1,
        )
        self.smoke(
            "ContainerlabBaseAPI.get_host_ip",
            lambda: api.get_host_ip(CLIENT1, CLIENT_INTF),
            expect_type=str,
            min_len=7,
        )
        self.smoke(
            "ContainerlabBaseAPI.netstat", lambda: api.netstat(CLIENT1), min_len=1
        )
        self.smoke(
            "ContainerlabBaseAPI.ip_addr_statistics",
            lambda: api.ip_addr_statistics(CLIENT1),
            min_len=1,
        )
        self.smoke(
            "ContainerlabBaseAPI.ethtool",
            lambda: api.ethtool(CLIENT1, CLIENT_INTF),
            min_len=1,
        )
        self.smoke(
            "ContainerlabBaseAPI.tc_show_statistics",
            lambda: api.tc_show_statistics(CLIENT1, CLIENT_INTF),
            min_len=1,
        )

    def test_containerlab_reachability_json(self) -> None:
        api = self._host_api()

        async def _reachability() -> str:
            return await api.get_reachability()

        payload = self.smoke_async(
            "ContainerlabBaseAPI.get_reachability", _reachability, min_len=2
        )
        parsed = assert_json_payload("ContainerlabBaseAPI.get_reachability", payload)
        assert "results" in parsed
        assert parsed["results"]

    def test_containerlab_srl_api(self) -> None:
        api = self._srl_api()
        assert api.uses_srl_router(LEAF1)
        assert not api.uses_srl_router(CLIENT1)
        asn = self.smoke(
            "ContainerlabSRLAPI.srl_get_bgp_as",
            lambda: api.srl_get_bgp_as(LEAF1),
            expect_type=int,
        )
        assert asn > 0
        self.smoke(
            "ContainerlabSRLAPI.srl_exec_cli(version)",
            lambda: api.srl_exec_cli(LEAF1, "show version"),
            min_len=1,
        )
        self.smoke(
            "ContainerlabSRLAPI.srl_exec_cli(running-config)",
            lambda: api.srl_exec_cli(LEAF1, "info from running"),
            min_len=1,
        )
        self.smoke(
            "ContainerlabSRLAPI.srl_exec_cli(bgp-summary)",
            lambda: api.srl_exec_cli(
                LEAF1, "show network-instance default protocols bgp summary"
            ),
            min_len=1,
        )
        self.smoke(
            "ContainerlabSRLAPI.srl_exec_cli(ip-route)",
            lambda: api.srl_exec_cli(
                LEAF1, "show network-instance default route-table ipv4"
            ),
            min_len=1,
        )
        self.smoke(
            "ContainerlabSRLAPI.srl_bgp_acl_drop_179_present",
            lambda: api.srl_bgp_acl_drop_179_present(LEAF1),
            expect_type=bool,
        )
        assert (
            self.smoke(
                "ContainerlabSRLAPI.srl_get_bgp_as(re-check)",
                lambda: api.srl_get_bgp_as(LEAF1),
                expect_type=int,
            )
            == asn
        )
