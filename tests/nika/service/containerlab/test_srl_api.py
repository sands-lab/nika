from __future__ import annotations

import pytest
from unittest.mock import MagicMock
from nika.service.containerlab.adapters import LabRuntimeContainerlabAPI
from nika.service.containerlab.srl_api import BGP_EXPORT_POLICY, SRLAPIMixin

pytestmark = pytest.mark.integration

# Excerpt of ``info flat from running acl acl-filter cpm type ipv4`` on an
# SR Linux 24.10.7 containerlab node (isp_pdh n1).
_CPM_DEFAULT = """\
set / acl acl-filter cpm type ipv4 statistics-per-entry true
set / acl acl-filter cpm type ipv4 entry 60 description "Accept incoming SSH when the other host initiates the TCP connection"
set / acl acl-filter cpm type ipv4 entry 60 match ipv4 protocol tcp
set / acl acl-filter cpm type ipv4 entry 60 match transport destination-port operator eq
set / acl acl-filter cpm type ipv4 entry 60 match transport destination-port value 22
set / acl acl-filter cpm type ipv4 entry 60 action accept
set / acl acl-filter cpm type ipv4 entry 70 description "Accept incoming SSH when this router initiates the TCP connection"
set / acl acl-filter cpm type ipv4 entry 70 match ipv4 protocol tcp
set / acl acl-filter cpm type ipv4 entry 70 match transport source-port operator eq
set / acl acl-filter cpm type ipv4 entry 70 match transport source-port value 22
set / acl acl-filter cpm type ipv4 entry 70 action accept
set / acl acl-filter cpm type ipv4 entry 88 description "Containerlab-added rule: Accept incoming Telnet when the other host initiates the TCP connection"
set / acl acl-filter cpm type ipv4 entry 88 match ipv4 protocol tcp
set / acl acl-filter cpm type ipv4 entry 88 match transport source-port operator eq
set / acl acl-filter cpm type ipv4 entry 88 match transport source-port value 23
set / acl acl-filter cpm type ipv4 entry 88 action accept
set / acl acl-filter cpm type ipv4 entry 170 description "Accept incoming NTP messages from servers"
set / acl acl-filter cpm type ipv4 entry 170 match ipv4 protocol udp
set / acl acl-filter cpm type ipv4 entry 170 match transport source-port operator eq
set / acl acl-filter cpm type ipv4 entry 170 match transport source-port value 123
set / acl acl-filter cpm type ipv4 entry 170 action accept
set / acl acl-filter cpm type ipv4 entry 180 description "Accept incoming SNMP GET/GETNEXT messages from servers"
set / acl acl-filter cpm type ipv4 entry 180 match ipv4 protocol udp
set / acl acl-filter cpm type ipv4 entry 180 match transport destination-port operator eq
set / acl acl-filter cpm type ipv4 entry 180 match transport destination-port value 161
set / acl acl-filter cpm type ipv4 entry 180 action accept
set / acl acl-filter cpm type ipv4 entry 190 description "Accept incoming BGP when the other router initiates the TCP connection"
set / acl acl-filter cpm type ipv4 entry 190 match ipv4 protocol tcp
set / acl acl-filter cpm type ipv4 entry 190 match transport destination-port operator eq
set / acl acl-filter cpm type ipv4 entry 190 match transport destination-port value 179
set / acl acl-filter cpm type ipv4 entry 190 action accept
set / acl acl-filter cpm type ipv4 entry 200 description "Accept incoming BGP when this router initiates the TCP connection"
set / acl acl-filter cpm type ipv4 entry 200 match ipv4 protocol tcp
set / acl acl-filter cpm type ipv4 entry 200 match transport source-port operator eq
set / acl acl-filter cpm type ipv4 entry 200 match transport source-port value 179
set / acl acl-filter cpm type ipv4 entry 200 action accept
set / acl acl-filter cpm type ipv4 entry 1000 description "Drop all else"
set / acl acl-filter cpm type ipv4 entry 1000 action log true
set / acl acl-filter cpm type ipv4 entry 1000 action drop
"""

# Same node family (isp_pdh n2) after committing an extra BGP accept at entry 75.
_CPM_ACCEPT_75 = (
    _CPM_DEFAULT
    + """\
set / acl acl-filter cpm type ipv4 entry 75 description "Accept BGP moved"
set / acl acl-filter cpm type ipv4 entry 75 match ipv4 protocol tcp
set / acl acl-filter cpm type ipv4 entry 75 match transport source-port operator eq
set / acl acl-filter cpm type ipv4 entry 75 match transport source-port value 179
set / acl acl-filter cpm type ipv4 entry 75 action accept
"""
)

# isp_pdh n1 after ``srl_add_bgp_acl_drop_179``.
_CPM_INJECTED = (
    _CPM_DEFAULT
    + """\
set / acl acl-filter cpm type ipv4 entry 185 description cpm-protect
set / acl acl-filter cpm type ipv4 entry 185 match ipv4 protocol tcp
set / acl acl-filter cpm type ipv4 entry 185 match transport destination-port operator eq
set / acl acl-filter cpm type ipv4 entry 185 match transport destination-port value 179
set / acl acl-filter cpm type ipv4 entry 185 action drop
set / acl acl-filter cpm type ipv4 entry 186 description cpm-protect
set / acl acl-filter cpm type ipv4 entry 186 match ipv4 protocol tcp
set / acl acl-filter cpm type ipv4 entry 186 match transport source-port operator eq
set / acl acl-filter cpm type ipv4 entry 186 match transport source-port value 179
set / acl acl-filter cpm type ipv4 entry 186 action drop
"""
)

_DEFAULT_DROPS = {185: "destination-port", 186: "source-port"}


class _SRLStub(SRLAPIMixin):
    backend = "containerlab"

    def __init__(self) -> None:
        self.exec_calls: list[tuple[str, str]] = []

    def exec_cmd(self, host_name: str, command: str, timeout: float = 10) -> str:
        self.exec_calls.append((host_name, command))
        return ""


class SrlApiTest:
    def test_uses_srl_router_true_when_sr_cli_present(self) -> None:
        api = _SRLStub()
        api.exec_cmd = MagicMock(return_value="/usr/bin/sr_cli")

        assert api.uses_srl_router("leaf1")

    def test_uses_srl_router_false_on_kathara(self) -> None:
        api = _SRLStub()
        api.backend = "kathara"

        assert not api.uses_srl_router("router1")

    def test_srl_get_bgp_as_parses_output(self) -> None:
        runtime = MagicMock()
        runtime.backend = "containerlab"
        runtime.exec.return_value = "Global AS number  : 65001"
        adapter = LabRuntimeContainerlabAPI(runtime)

        assert adapter.srl_get_bgp_as("leaf1") == 65001

    def test_srl_exec_cli_uses_quoted_command(self) -> None:
        runtime = MagicMock()
        runtime.backend = "containerlab"
        runtime.exec.return_value = "ok"
        adapter = LabRuntimeContainerlabAPI(runtime)
        adapter.srl_exec_cli("leaf1", "show version")
        runtime.exec.assert_called_once()
        cmd = runtime.exec.call_args[0][1]

        assert 'sr_cli "show version"' in cmd

    def test_srl_bgp_acl_entries_precede_default_bgp_accept(self) -> None:
        api = _SRLStub()
        api.exec_cmd = MagicMock(return_value=_CPM_DEFAULT)

        assert api.srl_bgp_acl_drop_179_entries("n1") == _DEFAULT_DROPS

    def test_srl_bgp_acl_entries_precede_lowest_bgp_accept(self) -> None:
        api = _SRLStub()
        api.exec_cmd = MagicMock(return_value=_CPM_ACCEPT_75)

        assert api.srl_bgp_acl_drop_179_entries("n2") == {
            72: "destination-port",
            73: "source-port",
        }

    def test_srl_bgp_acl_entries_fail_without_free_ids(self) -> None:
        api = _SRLStub()
        api.exec_cmd = MagicMock(
            return_value=_CPM_DEFAULT.replace("entry 180 ", "entry 1 ").replace(
                "entry 190 ", "entry 2 "
            )
        )

        with pytest.raises(ValueError, match="before entry 2"):
            api.srl_bgp_acl_drop_179_entries("n1")

    def test_srl_add_bgp_acl_drop_179_uses_chosen_entries(self) -> None:
        api = _SRLStub()
        api.exec_cmd = MagicMock(
            side_effect=[
                _CPM_ACCEPT_75,
                "",
                "set / network-instance default protocols bgp neighbor "
                "10.255.0.1 peer-group ibgp",
                "",
            ]
        )

        assert api.srl_add_bgp_acl_drop_179("n2") == {
            72: "destination-port",
            73: "source-port",
        }
        candidate = api.exec_cmd.call_args_list[1][0][1]
        assert "entry 72 match transport destination-port value 179" in candidate
        assert "entry 73 match transport source-port value 179" in candidate
        assert "entry 72 action drop" in candidate
        reset = api.exec_cmd.call_args_list[3][0][1]
        assert "neighbor 10.255.0.1 reset-peer" in reset

    def test_srl_ospf_drop_precedes_the_ospf_accept_entry(self) -> None:
        # SR Linux 24.10 default CPM filter: entry 290 accepts protocol 89.
        accept = (
            _CPM_DEFAULT
            + "set / acl acl-filter cpm type ipv4 entry 290 match ipv4 protocol 89\n"
            "set / acl acl-filter cpm type ipv4 entry 290 action accept\n"
        )
        api = _SRLStub()
        api.exec_cmd = MagicMock(side_effect=[accept, ""])

        assert api.srl_add_ospf_acl_drop("n1") == [245]
        candidate = api.exec_cmd.call_args_list[1][0][1]
        assert "entry 245 match ipv4 protocol 89" in candidate
        assert "entry 245 action drop" in candidate

    def test_srl_ospf_neighbors_and_interfaces_parse_flat_output(self) -> None:
        # ``info flat from state|running`` on isp_abilene chinng (SR Linux 24.10).
        state = (
            "/ network-instance default protocols ospf instance default area "
            "0.0.0.0 interface ethernet-1/1.0 neighbor 10.255.0.6 adjacency-state full\n"
            "/ network-instance default protocols ospf instance default area "
            "0.0.0.0 interface ethernet-1/2.0 neighbor 10.255.0.9 adjacency-state init\n"
        )
        running = (
            "set / network-instance default protocols ospf instance default area "
            "0.0.0.0 interface ethernet-1/1.0 metric 10\n"
            "set / network-instance default protocols ospf instance default area "
            "0.0.0.0 interface system0.0 passive true\n"
        )
        api = _SRLStub()
        api.exec_cmd = MagicMock(side_effect=[state, running, running, ""])

        assert api.srl_ospf_neighbor_interfaces("n1") == ["ethernet-1/1.0"]
        assert api.srl_ospf_interfaces("n1") == ["ethernet-1/1.0"]
        api.srl_remove_ospf_interface("n1", "ethernet-1/1.0")
        assert (
            "delete /network-instance default protocols ospf instance default "
            "area 0.0.0.0 interface ethernet-1/1.0"
        ) in api.exec_cmd.call_args_list[3][0][1]

    def test_srl_bgp_acl_present(self) -> None:
        runtime = MagicMock()
        runtime.backend = "containerlab"
        runtime.exec.return_value = _CPM_INJECTED
        adapter = LabRuntimeContainerlabAPI(runtime)

        assert adapter.srl_bgp_acl_drop_179_present("n1", _DEFAULT_DROPS)
        assert not adapter.srl_bgp_acl_drop_179_present(
            "n1", {72: "destination-port", 73: "source-port"}
        )
        runtime.exec.return_value = _CPM_DEFAULT
        assert not adapter.srl_bgp_acl_drop_179_present("n1", _DEFAULT_DROPS)

    def test_srl_withdraw_bgp_prefix_uses_candidate(self) -> None:
        api = _SRLStub()
        api.srl_exec_cli = lambda *args, **kwargs: (
            '{"network-instance":[{"protocols":{"bgp":{"neighbor":[{"peer-address":"10.0.0.1","afi-safi":[{"afi-safi-name":"ipv4-unicast","export-policy":["BGP-OUT"]}]}]}}}]}'
        )
        api.srl_withdraw_bgp_prefix("leaf1", "10.0.0.24/31")

        assert len(api.exec_calls) == 1
        script = api.exec_calls[0][1]

        assert "enter candidate" in script

        assert BGP_EXPORT_POLICY in script

        assert "10.0.0.24/31" in script

        assert "default-action policy-result next-policy" in script
        assert (
            f"neighbor 10.0.0.1 afi-safi ipv4-unicast export-policy [{BGP_EXPORT_POLICY} BGP-OUT]"
            in script
        )

        assert "match prefix-set" in script

        assert "export-policy" in script

    def test_srl_bgp_prefix_withdrawn(self) -> None:
        runtime = MagicMock()
        runtime.backend = "containerlab"
        runtime.exec.side_effect = [
            f"policy {BGP_EXPORT_POLICY} prefix 10.0.0.24/31",
            f"group clos01 export-policy {BGP_EXPORT_POLICY}",
        ]
        adapter = LabRuntimeContainerlabAPI(runtime)

        assert adapter.srl_bgp_prefix_withdrawn("leaf1", "10.0.0.24/31")
