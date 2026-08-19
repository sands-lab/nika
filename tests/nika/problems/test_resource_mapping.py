from __future__ import annotations

import pytest

from nika.problems.ground_truth import (
    build_ground_truth,
    build_multi_ground_truth,
    ground_truth_for_case,
)
from nika.problems.prob_pool import (
    list_avail_problem_instances,
    list_avail_problem_names,
)
from nika.problems.root_cause import UnresolvedRootCauseError
from nika.problems.problem_base import FailureDomain


EXPECTED_FAILURE_DOMAINS = {
    FailureDomain.LINK_INTERFACE: {
        "link_detach",
        "link_down",
        "link_flap",
        "link_high_packet_corruption",
    },
    FailureDomain.ROUTING_CONTROL_PLANE: {
        "bgp_asn_misconfig",
        "bgp_blackhole_route_leak",
        "bgp_hijacking",
        "bgp_max_prefix_exceeded",
        "bgp_missing_route_advertisement",
        "bgp_rpki_invalid_route_leak",
        "frr_service_down",
        "ospf_area_misconfiguration",
        "ospf_neighbor_missing",
    },
    FailureDomain.FORWARDING_ENCAPSULATION_POLICY: {
        "arp_acl_block",
        "bgp_acl_block",
        "bmv2_switch_down",
        "dns_port_blocked",
        "flow_rule_loop",
        "flow_rule_shadowing",
        "host_static_blackhole",
        "http_acl_block",
        "icmp_acl_block",
        "k8s_networkpolicy_deny",
        "mpls_label_limit_exceeded",
        "mtu_mismatch",
        "ospf_acl_block",
        "p4_aggressive_detection_thresholds",
        "p4_compilation_error_parser_state",
        "p4_header_definition_error",
        "p4_table_entry_misconfig",
        "p4_table_entry_missing",
        "p4_action_selector_member_misconfig",
        "p4_ecmp_group_member_missing",
        "p4runtime_pipeline_mismatch",
        "p4runtime_partial_write",
        "p4_table_resource_exhaustion",
        "vrf_dscp_remarking",
        "wireguard_allowed_ips_misconfiguration",
        "wireguard_peer_key_misconfiguration",
    },
    FailureDomain.SERVICE_NETWORKING: {
        "k8s_clusterip_routing_broken",
        "load_balancer_overload",
    },
    FailureDomain.MANAGEMENT_ORCHESTRATION_PLANE: {
        "k8s_worker_apiserver_partition",
        "sdn_controller_crash",
        "southbound_port_block",
        "southbound_port_mismatch",
    },
    FailureDomain.ADDRESSING_NEIGHBOR_NAMING: {
        "arp_cache_poisoning",
        "dhcp_missing_subnet",
        "dhcp_service_down",
        "dhcp_spoofed_dns",
        "dhcp_spoofed_gateway",
        "dhcp_spoofed_subnet",
        "dns_lookup_latency",
        "dns_record_error",
        "dns_service_down",
        "host_incorrect_dns",
        "host_incorrect_gateway",
        "host_incorrect_ip",
        "host_incorrect_netmask",
        "host_ip_conflict",
        "host_missing_ip",
        "k8s_coredns_isolated",
        "mac_address_conflict",
    },
    FailureDomain.ENDPOINT_APPLICATION: {
        "host_crash",
        "receiver_resource_contention",
        "sender_application_delay",
        "sender_resource_contention",
        "web_dos_attack",
    },
    FailureDomain.TRAFFIC_QUEUEING_RESOURCE: {
        "incast_traffic_network_limitation",
        "link_bandwidth_throttling",
    },
}


class _Spec:
    def __init__(self, endpoints: tuple[str, ...]):
        self.links = [type("Link", (), {"endpoints": endpoints})()]


class _Env:
    def __init__(self, endpoints: tuple[str, ...], backend: str = "kathara"):
        self.backend = backend
        self.lab = None
        self._spec = _Spec(endpoints)

    def get_lab_spec(self):
        return self._spec


def _resources(problem: str, params: dict, env: _Env):
    return (
        ground_truth_for_case(
            problem=problem,
            params=params,
            scenario="simple_bgp",
            net_env=env,
        )
        .root_causes[0]
        .resource
    )


class ResourceMappingTest:
    def test_every_failure_declares_failure_domain(self) -> None:
        problems = list_avail_problem_instances()
        assert len(problems) == 69
        for name, cls in problems.items():
            assert cls.META is not None, name
            assert set(cls.taxonomy_metadata()) == {"failure_domain"}
            assert cls.__module__.split(".")[-2] == cls.META.failure_domain, name

        actual = {
            domain: {
                name
                for name, cls in problems.items()
                if cls.META is not None and cls.META.failure_domain == domain
            }
            for domain in FailureDomain
        }
        assert actual == EXPECTED_FAILURE_DOMAINS

    def test_every_failure_implements_mapping(self) -> None:
        missing = [
            name
            for name, cls in list_avail_problem_instances().items()
            if "root_cause_resources" not in cls.__dict__
        ]
        assert missing == [], missing
        assert set(list_avail_problem_names())

    def test_link_down_is_single_interface(self) -> None:
        env = _Env(("pc1:eth0", "router1:eth0"))
        resource = _resources(
            "link_down", {"host_name": "pc1", "intf_name": "eth0"}, env
        )
        assert resource.id == "interface/pc1/eth0"

    def test_link_quality_is_interface_even_on_lan(self) -> None:
        env = _Env(
            ("pc1:eth0", "pc2:eth0", "r1:eth0", "a:eth0", "b:eth0", "c:eth0", "d:eth0")
        )
        resource = _resources("link_high_packet_corruption", {"host_name": "pc1"}, env)
        assert resource.kind == "interface"
        assert resource.id.startswith("interface/pc1/")

    def test_dhcp_client_is_not_a_root_cause(self) -> None:
        env = _Env(("pc1:eth0", "dhcp:eth0"))
        resource = _resources(
            "dhcp_missing_subnet",
            {"host_name": "dhcp", "host_name_2": "pc1"},
            env,
        )
        assert resource.id == "node/dhcp"

    def test_ip_conflict_marks_mutated_interface_only(self) -> None:
        env = _Env(("pc1:eth0", "pc2:eth0"))
        resource = _resources(
            "host_ip_conflict",
            {"host_name": "pc1", "host_name_2": "pc2"},
            env,
        )
        assert resource.id == "interface/pc2/eth0"

    def test_host_vpn_alias_maps_to_wireguard_interface(self) -> None:
        env = _Env(("br1_edge:eth0", "hq_edge:eth0"))
        # Legacy id resolves to wireguard_peer_key_misconfiguration.
        resource = _resources(
            "host_vpn_membership_missing",
            {"host_name": "br1_edge", "intf_name": "wg_hq"},
            env,
        )
        assert resource.id == "interface/br1_edge/wg_hq"

    def test_wireguard_peer_key_is_wg_interface(self) -> None:
        env = _Env(("br1_edge:eth0", "hq_edge:eth0"))
        resource = _resources(
            "wireguard_peer_key_misconfiguration",
            {"host_name": "br1_edge", "intf_name": "wg_hq"},
            env,
        )
        assert resource.id == "interface/br1_edge/wg_hq"

    def test_wireguard_allowed_ips_is_wg_interface(self) -> None:
        env = _Env(("br1_edge:eth0", "hq_edge:eth0"))
        resource = _resources(
            "wireguard_allowed_ips_misconfiguration",
            {
                "host_name": "br1_edge",
                "intf_name": "wg_hq",
                "target_prefix": "10.0.20.0/24",
            },
            env,
        )
        assert resource.id == "interface/br1_edge/wg_hq"

    def test_resources_follow_inject_not_side_effects(self) -> None:
        env = _Env(("br1_edge:eth0", "hq_edge:eth0"))
        from nika.problems.prob_pool import get_problem_class

        cls = get_problem_class("host_vpn_membership_missing")
        assert cls is not None
        assert cls.root_cause_name == "wireguard_peer_key_misconfiguration"
        problem = cls.__new__(cls)
        problem.net_env = env
        problem.parse_params({"host_name": "br1_edge", "intf_name": "wg_hq"})
        resources = problem.root_cause_resources(problem._resolved_params)
        assert [r.node for r in resources] == ["br1_edge"]

    def test_networkpolicy_is_k8s(self) -> None:
        env = _Env(("n1:eth0", "n2:eth0"))
        resource = _resources(
            "k8s_networkpolicy_deny",
            {
                "namespace": "word-ns",
                "policy_name": "nika-deny-ingress",
                "pod_selector": "app=word",
                "symptom_url": "http://datacenter.com/word",
                "control_url": "http://datacenter.com/weather",
            },
            env,
        )
        assert resource.id == "k8s/NetworkPolicy/word-ns/nika-deny-ingress"

    def test_coredns_is_kube_dns_service(self) -> None:
        env = _Env(("n1:eth0", "n2:eth0"))
        resource = _resources("k8s_coredns_isolated", {"control_node": "n1"}, env)
        assert resource.id == "k8s/Service/kube-system/kube-dns"

    def test_web_dos_is_victim_node(self) -> None:
        env = _Env(("web:eth0", "atk:eth0"))
        resource = _resources(
            "web_dos_attack",
            {"host_name": "web1", "attacker_device": "pc1"},
            env,
        )
        assert resource.id == "node/web1"

    def test_build_ground_truth_one_object(self) -> None:
        env = _Env(("pc1:eth0", "r1:eth0"))
        from nika.problems.prob_pool import get_problem_class

        cls = get_problem_class("link_down")
        assert cls is not None
        problem = cls.__new__(cls)
        problem.net_env = env
        problem._resolved_params = None
        gt = build_ground_truth(problem, {"host_name": "pc1", "intf_name": "eth0"}, env)
        assert gt.is_anomaly
        assert len(gt.root_causes) == 1
        assert gt.root_causes[0].fault_type == "link_down"
        assert gt.root_causes[0].resource is not None
        assert gt.root_causes[0].resource.node == "pc1"
        assert gt.schema_version == 3
        assert gt.failure_domain == "link_interface"

    def test_multi_root_cause(self) -> None:
        env = _Env(("pc1:eth0", "r1:eth0"))
        from nika.problems.prob_pool import get_problem_class

        def _piece(name: str, params: dict):
            cls = get_problem_class(name)
            assert cls is not None
            inst = cls.__new__(cls)
            inst.net_env = env
            inst.parse_params(params)
            return inst

        gt = build_multi_ground_truth(
            [
                _piece("link_down", {"host_name": "pc1", "intf_name": "eth0"}),
                _piece("host_crash", {"host_name": "pc1"}),
            ],
            failure_domain="multiple_faults",
        )
        assert len(gt.root_causes) == 2
        assert {item.fault_type for item in gt.root_causes} == {
            "link_down",
            "host_crash",
        }

    def test_unknown_failure_unresolved(self) -> None:
        with pytest.raises(UnresolvedRootCauseError):
            ground_truth_for_case(
                problem="not_a_real_fault",
                params={},
                scenario="simple_bgp",
                net_env=_Env(("a:eth0", "b:eth0")),
            )


class OfflineCaseTruthTest:
    def test_simple_bgp_link_down(self) -> None:
        gt = ground_truth_for_case(
            problem="link_down",
            params={"host_name": "pc1", "intf_name": "eth0"},
            scenario="simple_bgp",
        )
        assert gt.root_causes[0].resource.id == "interface/pc1/eth0"
