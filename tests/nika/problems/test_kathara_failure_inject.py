from __future__ import annotations
from tests.support.integration_base import PerTestEnvTestCase

HOST = "pc1"
HOST2 = "pc2"
INTF = "eth0"
LINK_PARAMS = {"host_name": HOST, "intf_name": INTF}


class LinkFailureVerifyTest(PerTestEnvTestCase):
    SCENARIO = "simple_bgp"

    def test_link_down(self) -> None:
        self._inject_failure("link_down", LINK_PARAMS)
        self._assert_failure_injected("link_down")

    def test_link_flap(self) -> None:
        self._inject_failure("link_flap", LINK_PARAMS)
        self._assert_failure_injected("link_flap")

    def test_link_detach(self) -> None:
        self._inject_failure("link_detach", LINK_PARAMS)
        self._assert_failure_injected("link_detach")

    def test_mtu_mismatch(self) -> None:
        self._inject_failure("mtu_mismatch", {"host_name": HOST})
        self._assert_failure_injected("mtu_mismatch")

    def test_link_fragmentation_disabled_is_alias(self) -> None:
        from nika.problems.prob_pool import get_problem_class, resolve_problem_name

        assert resolve_problem_name("link_fragmentation_disabled") == "mtu_mismatch"
        cls = get_problem_class("link_fragmentation_disabled")
        assert cls is not None
        assert cls.root_cause_name == "mtu_mismatch"


class HostMisconfigVerifyTest(PerTestEnvTestCase):
    SCENARIO = "simple_bgp"

    def test_host_missing_ip(self) -> None:
        self._inject_failure("host_missing_ip", {"host_name": HOST, "intf_name": INTF})
        self._assert_failure_injected("host_missing_ip")

    def test_host_ip_conflict(self) -> None:
        self._inject_failure(
            "host_ip_conflict", {"host_name": HOST, "host_name_2": HOST2}
        )
        self._assert_failure_injected("host_ip_conflict")

    def test_host_incorrect_ip(self) -> None:
        self._inject_failure("host_incorrect_ip", {"host_name": HOST})
        self._assert_failure_injected("host_incorrect_ip")

    def test_host_incorrect_gateway(self) -> None:
        self._inject_failure("host_incorrect_gateway", {"host_name": HOST})
        self._assert_failure_injected("host_incorrect_gateway")

    def test_host_incorrect_netmask(self) -> None:
        self._inject_failure("host_incorrect_netmask", {"host_name": HOST})
        self._assert_failure_injected("host_incorrect_netmask")


class HostIncorrectDNSVerifyTest(PerTestEnvTestCase):
    SCENARIO = "campus_lan"
    ENV_RUN_ARGS = ["-s", "s", "--workload", "dhcp"]

    def test_host_incorrect_dns(self) -> None:
        self._inject_failure("host_incorrect_dns")
        self._assert_failure_injected("host_incorrect_dns")

    def test_dns_port_blocked(self) -> None:
        self._inject_failure("dns_port_blocked")
        self._assert_failure_injected("dns_port_blocked")


class OSPFMisconfigVerifyTest(PerTestEnvTestCase):
    SCENARIO = "campus_lan"
    ENV_RUN_ARGS = ["-s", "s", "--workload", "static"]

    def test_ospf_area_misconfiguration(self) -> None:
        self._inject_failure("ospf_area_misconfiguration")
        self._assert_failure_injected("ospf_area_misconfiguration")

    def test_ospf_neighbor_missing(self) -> None:
        self._inject_failure("ospf_neighbor_missing")
        self._assert_failure_injected("ospf_neighbor_missing")

    def test_ospf_acl_block(self) -> None:
        self._inject_failure("ospf_acl_block")
        self._assert_failure_injected("ospf_acl_block")


class BGPMisconfigVerifyTest(PerTestEnvTestCase):
    SCENARIO = "simple_bgp"

    def test_bgp_asn_misconfig(self) -> None:
        self._inject_failure("bgp_asn_misconfig")
        self._assert_failure_injected("bgp_asn_misconfig")

    def test_bgp_missing_route_advertisement(self) -> None:
        self._inject_failure("bgp_missing_route_advertisement")
        self._assert_failure_injected("bgp_missing_route_advertisement")

    def test_host_static_blackhole(self) -> None:
        self._inject_failure("host_static_blackhole")
        self._assert_failure_injected("host_static_blackhole")

    def test_bgp_blackhole_route_leak(self) -> None:
        self._inject_failure("bgp_blackhole_route_leak")
        self._assert_failure_injected("bgp_blackhole_route_leak")


class MacMisconfigVerifyTest(PerTestEnvTestCase):
    SCENARIO = "campus_lan"
    ENV_RUN_ARGS = ["-s", "s", "--workload", "static"]

    def test_mac_address_conflict(self) -> None:
        self._inject_failure("mac_address_conflict")
        self._assert_failure_injected("mac_address_conflict")


class DHCPMisconfigVerifyTest(PerTestEnvTestCase):
    SCENARIO = "campus_lan"
    ENV_RUN_ARGS = ["-s", "s", "--workload", "dhcp"]

    def test_dhcp_missing_subnet(self) -> None:
        self._inject_failure("dhcp_missing_subnet")
        self._assert_failure_injected("dhcp_missing_subnet")


class ACLBlockVerifyTest(PerTestEnvTestCase):
    SCENARIO = "simple_bgp"

    def test_bgp_acl_block(self) -> None:
        self._inject_failure("bgp_acl_block")
        self._assert_failure_injected("bgp_acl_block")

    def test_icmp_acl_block(self) -> None:
        self._inject_failure("icmp_acl_block")
        self._assert_failure_injected("icmp_acl_block")

    def test_arp_acl_block(self) -> None:
        self._inject_failure("arp_acl_block")
        self._assert_failure_injected("arp_acl_block")


class HttpACLBlockVerifyTest(PerTestEnvTestCase):
    SCENARIO = "campus_lan"
    ENV_RUN_ARGS = ["-s", "s", "--workload", "dhcp"]

    def test_http_acl_block(self) -> None:
        self._inject_failure("http_acl_block")
        self._assert_failure_injected("http_acl_block")


class P4MisconfigVerifyTest(PerTestEnvTestCase):
    SCENARIO = "p4_bloom_filter"

    def test_p4_aggressive_detection_thresholds(self) -> None:
        self._inject_failure("p4_aggressive_detection_thresholds")
        self._assert_failure_injected("p4_aggressive_detection_thresholds")


class Bmv2SwitchDownVerifyTest(PerTestEnvTestCase):
    SCENARIO = "p4_counter"

    def test_bmv2_switch_down(self) -> None:
        self._inject_failure("bmv2_switch_down")
        self._assert_failure_injected("bmv2_switch_down")

    def test_p4_header_definition_error(self) -> None:
        self._inject_failure("p4_header_definition_error")
        self._assert_failure_injected("p4_header_definition_error")

    def test_p4_compilation_error_parser_state(self) -> None:
        self._inject_failure("p4_compilation_error_parser_state")
        self._assert_failure_injected("p4_compilation_error_parser_state")

    def test_p4_table_entry_missing(self) -> None:
        self._inject_failure("p4_table_entry_missing")
        self._assert_failure_injected("p4_table_entry_missing")

    def test_p4_table_entry_misconfig(self) -> None:
        self._inject_failure("p4_table_entry_misconfig")
        self._assert_failure_injected("p4_table_entry_misconfig")


class P4MPLSVerifyTest(PerTestEnvTestCase):
    SCENARIO = "p4_mpls"

    def test_mpls_label_limit_exceeded(self) -> None:
        self._inject_failure("mpls_label_limit_exceeded")
        self._assert_failure_injected("mpls_label_limit_exceeded")


class FrrDownVerifyTest(PerTestEnvTestCase):
    SCENARIO = "simple_bgp"

    def test_frr_service_down(self) -> None:
        self._inject_failure("frr_service_down")
        self._assert_failure_injected("frr_service_down")


class SDNControllerVerifyTest(PerTestEnvTestCase):
    SCENARIO = "sdn_star"
    ENV_RUN_ARGS = ["-s", "s"]

    def test_sdn_controller_crash(self) -> None:
        self._inject_failure("sdn_controller_crash")
        self._assert_failure_injected("sdn_controller_crash")

    def test_southbound_port_block(self) -> None:
        self._inject_failure("southbound_port_block")
        self._assert_failure_injected("southbound_port_block")

    def test_southbound_port_mismatch(self) -> None:
        self._inject_failure("southbound_port_mismatch")
        self._assert_failure_injected("southbound_port_mismatch")

    def test_flow_rule_shadowing(self) -> None:
        self._inject_failure("flow_rule_shadowing")
        self._assert_failure_injected("flow_rule_shadowing")

    def test_flow_rule_loop(self) -> None:
        self._inject_failure("flow_rule_loop")
        self._assert_failure_injected("flow_rule_loop")


class WebDoSVerifyTest(PerTestEnvTestCase):
    SCENARIO = "dc_clos"
    ENV_RUN_ARGS = ["-s", "s", "--workload", "service"]

    def test_web_dos_attack(self) -> None:
        self._inject_failure("web_dos_attack")
        self._assert_failure_injected("web_dos_attack")


class DHCPAttackVerifyTest(PerTestEnvTestCase):
    SCENARIO = "campus_lan"
    ENV_RUN_ARGS = ["-s", "s", "--workload", "dhcp"]

    def test_dhcp_spoofed_gateway(self) -> None:
        self._inject_failure("dhcp_spoofed_gateway")
        self._assert_failure_injected("dhcp_spoofed_gateway")

    def test_dhcp_spoofed_dns(self) -> None:
        self._inject_failure("dhcp_spoofed_dns")
        self._assert_failure_injected("dhcp_spoofed_dns")

    def test_dhcp_spoofed_subnet(self) -> None:
        self._inject_failure("dhcp_spoofed_subnet")
        self._assert_failure_injected("dhcp_spoofed_subnet")


class BGPHijackingVerifyTest(PerTestEnvTestCase):
    SCENARIO = "simple_bgp"

    def test_bgp_hijacking(self) -> None:
        self._inject_failure("bgp_hijacking")
        self._assert_failure_injected("bgp_hijacking")

    def test_arp_cache_poisoning(self) -> None:
        self._inject_failure("arp_cache_poisoning", {"host_name": HOST})
        self._assert_failure_injected("arp_cache_poisoning")


class StressVerifyTest(PerTestEnvTestCase):
    SCENARIO = "campus_lan"
    ENV_RUN_ARGS = ["-s", "s", "--workload", "dhcp"]

    def test_sender_resource_contention(self) -> None:
        self._inject_failure("sender_resource_contention")
        self._assert_failure_injected("sender_resource_contention")

    def test_receiver_resource_contention(self) -> None:
        self._inject_failure("receiver_resource_contention")
        self._assert_failure_injected("receiver_resource_contention")

    def test_load_balancer_overload(self) -> None:
        self._inject_failure("load_balancer_overload")
        self._assert_failure_injected("load_balancer_overload")

    def test_sender_application_delay(self) -> None:
        self._inject_failure("sender_application_delay")
        self._assert_failure_injected("sender_application_delay")


class DNSLookupLatencyVerifyTest(PerTestEnvTestCase):
    SCENARIO = "dc_clos"
    ENV_RUN_ARGS = ["-s", "s", "--workload", "service"]

    def test_dns_lookup_latency(self) -> None:
        self._inject_failure("dns_lookup_latency")
        self._assert_failure_injected("dns_lookup_latency")


class LinkIssueVerifyTest(PerTestEnvTestCase):
    SCENARIO = "simple_bgp"

    def test_link_high_packet_corruption(self) -> None:
        self._inject_failure("link_high_packet_corruption", {"host_name": HOST})
        self._assert_failure_injected("link_high_packet_corruption")

    def test_link_bandwidth_throttling(self) -> None:
        self._inject_failure("link_bandwidth_throttling", {"host_name": HOST})
        self._assert_failure_injected("link_bandwidth_throttling")


class IncastTrafficLimitationVerifyTest(PerTestEnvTestCase):
    SCENARIO = "dc_clos"
    ENV_RUN_ARGS = ["-s", "s", "--workload", "service"]

    def test_incast_traffic_network_limitation(self) -> None:
        self._inject_failure("incast_traffic_network_limitation")
        self._assert_failure_injected("incast_traffic_network_limitation")


class DNSRecordErrorVerifyTest(PerTestEnvTestCase):
    SCENARIO = "campus_lan"
    ENV_RUN_ARGS = ["-s", "s", "--workload", "dhcp"]

    def test_dns_record_error(self) -> None:
        self._inject_failure("dns_record_error")
        self._assert_failure_injected("dns_record_error")


class HostCrashVerifyTest(PerTestEnvTestCase):
    SCENARIO = "simple_bgp"

    def test_host_crash(self) -> None:
        self._inject_failure("host_crash", {"host_name": HOST})
        self._assert_failure_injected("host_crash")


class VPNMembershipMissingVerifyTest:
    """Removed: legacy id remaps to wireguard_peer_key_misconfiguration."""

    def test_host_vpn_membership_missing_is_alias(self) -> None:
        from nika.problems.prob_pool import get_problem_class, resolve_problem_name

        assert (
            resolve_problem_name("host_vpn_membership_missing")
            == "wireguard_peer_key_misconfiguration"
        )
        cls = get_problem_class("host_vpn_membership_missing")
        assert cls is not None
        assert cls.root_cause_name == "wireguard_peer_key_misconfiguration"


class WireGuardAllowedIpsMisconfigVerifyTest(PerTestEnvTestCase):
    """Site Edge AllowedIPs omit of one remote prefix on enterprise_branch (small)."""

    SCENARIO = "enterprise_branch"
    ENV_RUN_ARGS = ["-s", "s"]

    def test_wireguard_allowed_ips_misconfiguration(self) -> None:
        import time

        from nika.net_env.verify import http_ok, ping_ok
        from nika.problems.prob_pool import get_problem_class

        params = {
            "host_name": "br1_edge",
            "intf_name": "wg_hq",
            "target_prefix": "10.0.20.0/24",
        }
        self._inject_failure("wireguard_allowed_ips_misconfiguration", params)
        self._assert_failure_injected("wireguard_allowed_ips_misconfiguration")

        cls = get_problem_class("wireguard_allowed_ips_misconfiguration")
        assert cls is not None
        problem = self._problem(cls)
        runtime = problem.runtime
        tunnels = {(t.spoke, t.spoke_iface): t for t in problem.net_env.built_tunnels}
        target = tunnels[("br1", "wg_hq")]
        other = tunnels[("br2", "wg_hq")]

        def _bgp_established(summary: str, peer_ip: str) -> bool:
            for line in summary.splitlines():
                fields = line.split()
                if peer_ip in line and len(fields) >= 10 and fields[9].isdigit():
                    return True
            return False

        # Underlay / WAN endpoint still reachable.
        assert ping_ok(runtime, "br1_edge", target.hub_wan_ip)
        assert ping_ok(runtime, "hq_edge", target.spoke_wan_ip)

        # Handshake still healthy; Hub tunnel address reachable over overlay.
        hs = runtime.exec("br1_edge", "wg show wg_hq latest-handshakes").strip()
        assert any(int(line.split()[-1]) > 0 for line in hs.splitlines() if line)
        assert ping_ok(runtime, "br1_edge", target.hub_tunnel_ip)

        # Overlay BGP stays Established; route to omitted prefix still present.
        br1_sum = runtime.exec("br1_edge", "vtysh -c 'show bgp summary'")
        br2_sum = runtime.exec("br2_edge", "vtysh -c 'show bgp summary'")
        assert _bgp_established(br1_sum, target.hub_tunnel_ip)
        assert _bgp_established(br2_sum, other.hub_tunnel_ip)
        rib = runtime.exec("br1_edge", "vtysh -c 'show ip route 10.0.20.0/24'")
        assert "10.0.20.0/24" in rib
        linux_route = runtime.exec("br1_edge", "ip route get 10.0.20.2").strip()
        assert "wg_hq" in linux_route

        conf = runtime.exec(
            "br1_edge", "grep -E '^AllowedIPs = ' /etc/wireguard/wg_hq.conf"
        ).strip()
        assert "10.0.20.0/24" not in conf
        assert f"{target.hub_tunnel_ip}/32" in conf
        assert "10.0.10.0/24" in conf

        # Target SERVER fails; CORP and other branch still succeed.
        assert not http_ok(runtime, "br1_corp_pc", "http://10.0.20.2/")
        assert not ping_ok(runtime, "br1_corp_pc", "10.0.20.2", count=2)
        assert ping_ok(runtime, "br1_corp_pc", "10.0.10.2")
        assert ping_ok(runtime, "br2_corp_pc", "10.0.10.2")
        assert http_ok(runtime, "br2_corp_pc", "http://10.0.20.2/")

        # Restore AllowedIPs → business recovers without BGP clear.
        runtime.exec(
            "br1_edge",
            "cp /etc/wireguard/wg_hq.conf.bak /etc/wireguard/wg_hq.conf "
            "&& wg-quick strip wg_hq > /tmp/wg_hq.strip "
            "&& wg syncconf wg_hq /tmp/wg_hq.strip",
        )
        deadline = time.monotonic() + 60
        recovered = False
        while time.monotonic() < deadline:
            if http_ok(runtime, "br1_corp_pc", "http://10.0.20.2/") and ping_ok(
                runtime, "br1_corp_pc", "10.0.20.2", count=1
            ):
                recovered = True
                break
            time.sleep(3)
        assert recovered, "HQ SERVER traffic did not recover after restoring AllowedIPs"


class WireGuardPeerKeyMisconfigVerifyTest(PerTestEnvTestCase):
    """Site Edge wrong Hub peer PublicKey on enterprise_branch (small)."""

    SCENARIO = "enterprise_branch"
    ENV_RUN_ARGS = ["-s", "s"]

    def test_wireguard_peer_key_misconfiguration(self) -> None:
        import time

        from nika.problems.misconfigurations.wireguard import WRONG_HUB_PEER_PUBLIC_KEY
        from nika.net_env.verify import http_ok, ping_ok
        from nika.problems.prob_pool import get_problem_class

        params = {"host_name": "br1_edge", "intf_name": "wg_hq"}
        self._inject_failure("wireguard_peer_key_misconfiguration", params)
        self._assert_failure_injected("wireguard_peer_key_misconfiguration")

        cls = get_problem_class("wireguard_peer_key_misconfiguration")
        assert cls is not None
        problem = self._problem(cls)
        runtime = problem.runtime
        br1_tunnels = [t for t in problem.net_env.built_tunnels if t.spoke == "br1"]
        assert len(br1_tunnels) >= 3
        tunnels = {(t.spoke, t.spoke_iface): t for t in problem.net_env.built_tunnels}
        target = tunnels[("br1", "wg_hq")]
        other = tunnels[("br2", "wg_hq")]
        wrong_key = WRONG_HUB_PEER_PUBLIC_KEY

        def _bgp_established(summary: str, peer_ip: str) -> bool:
            for line in summary.splitlines():
                fields = line.split()
                if peer_ip in line and len(fields) >= 10 and fields[9].isdigit():
                    return True
            return False

        # Underlay / WAN endpoint still reachable.
        assert ping_ok(runtime, "br1_edge", target.hub_wan_ip)
        assert ping_ok(runtime, "hq_edge", target.spoke_wan_ip)

        # Every WG iface on the spoke is up with wrong peer key and no handshake.
        br1_sum = runtime.exec("br1_edge", "vtysh -c 'show bgp summary'")
        for tunnel in br1_tunnels:
            iface = tunnel.spoke_iface
            link = runtime.exec("br1_edge", f"ip -o link show {iface}").strip()
            assert link and "state DOWN" not in link
            conf = runtime.exec(
                "br1_edge", f"grep -E '^PublicKey = ' /etc/wireguard/{iface}.conf"
            ).strip()
            assert conf == f"PublicKey = {wrong_key}"
            hs = runtime.exec("br1_edge", f"wg show {iface} latest-handshakes").strip()
            assert any(
                wrong_key in line and line.split()[-1] == "0"
                for line in hs.splitlines()
            )
            assert not _bgp_established(br1_sum, tunnel.hub_tunnel_ip)

        # Other branch stays up.
        br2_sum = runtime.exec("br2_edge", "vtysh -c 'show bgp summary'")
        assert _bgp_established(br2_sum, other.hub_tunnel_ip)

        # Target branch business fails; other branch succeeds.
        assert not ping_ok(runtime, "br1_corp_pc", "10.0.10.2", count=2)
        assert not http_ok(runtime, "br1_corp_pc", "http://10.0.20.2/")
        assert ping_ok(runtime, "br2_corp_pc", "10.0.10.2")
        assert http_ok(runtime, "br2_corp_pc", "http://10.0.20.2/")
        other_hs = runtime.exec("br2_edge", "wg show wg_hq latest-handshakes").strip()
        assert other_hs
        for line in other_hs.splitlines():
            assert int(line.split()[-1]) > 0

        # Restore correct keys on every iface → handshake + BGP + business recover.
        for tunnel in br1_tunnels:
            iface = tunnel.spoke_iface
            runtime.exec(
                "br1_edge",
                f"cp /etc/wireguard/{iface}.conf.bak /etc/wireguard/{iface}.conf "
                f"&& wg-quick strip {iface} > /tmp/{iface}.strip "
                f"&& wg syncconf {iface} /tmp/{iface}.strip "
                f"&& vtysh -c 'clear ip bgp {tunnel.hub_tunnel_ip}' "
                "2>/dev/null || true",
            )
        deadline = time.monotonic() + 90
        recovered = False
        while time.monotonic() < deadline:
            if ping_ok(runtime, "br1_corp_pc", "10.0.10.2", count=1) and http_ok(
                runtime, "br1_corp_pc", "http://10.0.20.2/"
            ):
                recovered = True
                break
            time.sleep(5)
        assert recovered, "cross-site traffic did not recover after restoring peer key"


class VrfDscpRemarkingVerifyTest(PerTestEnvTestCase):
    """CORP VRF DSCP EF→CS0 remarking on enterprise_branch (small)."""

    SCENARIO = "enterprise_branch"
    ENV_RUN_ARGS = ["-s", "s"]

    def test_vrf_dscp_remarking(self) -> None:
        """Require DSCP demotion and QoS regression under competing bulk load."""
        from nika.net_env.verify import http_ok, ping_ok
        from nika.problems.misconfigurations.vrf_dscp import VrfDscpRemarking
        from nika.problems.prob_pool import get_problem_class

        params = {
            "host_name": "br1_edge",
            "intf_name": "wg_hq",
            "src_host": "br1_corp_pc",
            "dst_host": "hq_corp_pc",
            "direction": "lan_to_overlay",
            "corp_prefix": "10.1.10.0/24",
        }
        cls = get_problem_class("vrf_dscp_remarking")
        assert cls is VrfDscpRemarking
        problem = self._problem(cls)
        parsed = problem.parse_params(params)

        problem.inject_fault(parsed)
        baseline = problem._baseline
        assert baseline is not None
        assert baseline.error is None, baseline
        assert baseline.jitter_ms is not None
        assert baseline.latency_ms is not None

        verify = problem.verify_fault(parsed)
        details = verify["details"]
        assert verify["verified"], details

        # Concrete fault symptoms (not just nft presence).
        assert details["samples_confirm"] is True
        assert details["src_tos"] == 0xB8
        assert details["dst_tos"] == 0
        assert details["edge_tos"] in (0, None)
        assert details["perf_degraded"] is True
        current = details["current"]
        assert current is not None
        assert (
            (current["jitter_ms"] or 0) >= max((baseline.jitter_ms or 0) * 5, 0.3)
            or (current["latency_ms"] or 0) >= max((baseline.latency_ms or 0) * 5, 5.0)
            or (current["latency_mdev_ms"] or 0)
            >= max((baseline.latency_mdev_ms or 0) * 5, 1.0)
            or (current["lost_percent"] or 0) >= (baseline.lost_percent or 0) + 10
            or (
                baseline.bits_per_second
                and current.get("bits_per_second") is not None
                and current["bits_per_second"] <= baseline.bits_per_second * 0.5
            )
        ), {"baseline": details["baseline"], "current": current}

        # Prefer seeing a large latency or loss move when available.
        if current.get("latency_ms") is not None and baseline.latency_ms is not None:
            assert current["latency_ms"] >= baseline.latency_ms * 3 or (
                current.get("lost_percent") or 0
            ) >= 10, {"baseline": details["baseline"], "current": current}

        runtime = problem.runtime
        assert ping_ok(runtime, "br2_corp_pc", "10.0.10.2")
        assert http_ok(runtime, "br2_corp_pc", "http://10.0.20.2/")
        assert ping_ok(runtime, "br1_corp_pc", "10.0.10.2")
        other_nft = runtime.exec(
            "br2_edge", "nft list table ip nika_dscp 2>/dev/null || true"
        )
        assert "nika_dscp" not in other_nft

        recovered = problem.recover_fault(parsed)
        assert recovered["verified"], recovered
        assert recovered["details"]["dscp_restored"]
        assert recovered["details"]["perf_restored"]
        assert recovered["details"]["dst_tos"] == 0xB8
        assert recovered["details"]["edge_tos"] in (0xB8, None)
        assert ping_ok(runtime, "br1_corp_pc", "10.0.10.2")
        assert http_ok(runtime, "br1_corp_pc", "http://10.0.20.2/")


class ServiceDownVerifyTest(PerTestEnvTestCase):
    SCENARIO = "campus_lan"
    ENV_RUN_ARGS = ["-s", "s", "--workload", "dhcp"]

    def test_dns_service_down(self) -> None:
        self._inject_failure("dns_service_down")
        self._assert_failure_injected("dns_service_down")

    def test_dhcp_service_down(self) -> None:
        self._inject_failure("dhcp_service_down")
        self._assert_failure_injected("dhcp_service_down")
