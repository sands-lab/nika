from __future__ import annotations

import pytest

from nika.problems.rca.materialize import (
    build_ground_truth,
    build_multi_ground_truth,
    ground_truth_for_case,
)
from nika.problems.registry import (
    get_problem_class,
    list_avail_problem_instances,
    list_avail_problem_names,
)
from nika.problems.base import FailureDomain
from nika.problems.rca import (
    FaultResource,
    RootCause,
    UnresolvedRootCauseError,
    canonical_root_causes,
    healthy_ground_truth,
    interface_resource,
    link_resource,
    node_resource,
    resource_from_id,
)
from nika.problems.rca.inventory import (
    canonical_link_name,
    catalog_resources,
    link_containing_endpoint,
)

EXPECTED_FAILURE_DOMAINS = {
    FailureDomain.LINK_INTERFACE: {
        "link_capacity_bottleneck",
        "link_detach",
        "link_down",
        "link_flap",
        "link_packet_corruption",
        "silent_egress_packet_loss",
    },
    FailureDomain.ROUTING_CONTROL_PLANE: {
        "bgp_asn_misconfig",
        "bgp_blackhole_route_leak",
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
        "icmp_frag_needed_filter_misconfiguration",
        "k8s_networkpolicy_deny",
        "mtu_mismatch",
        "ospf_acl_block",
        "p4_table_entry_misconfig",
        "p4_table_entry_missing",
        "p4_action_selector_member_misconfig",
        "p4_ecmp_group_member_missing",
        "p4runtime_pipeline_mismatch",
        "p4runtime_partial_write",
        "p4_table_resource_exhaustion",
        "p4_tcam_entry_corruption",
        "int_insufficient_mtu_headroom",
        "vrf_dscp_remarking",
        "wireguard_allowed_ips_misconfiguration",
        "wireguard_peer_key_misconfiguration",
        "device_forwarding_packet_corruption",
    },
    FailureDomain.SERVICE_NETWORKING: {
        "k8s_clusterip_routing_broken",
        "lb_connection_state_exhaustion",
        "lb_pending_connection_update_race",
        "load_balancer_overload",
        "nat_mapping_removed_without_drain",
        "snat_port_pool_exhaustion",
    },
    FailureDomain.MANAGEMENT_ORCHESTRATION_PLANE: {
        "k8s_worker_apiserver_partition",
        "sdn_controller_crash",
        "southbound_port_block",
        "southbound_port_mismatch",
    },
    FailureDomain.ADDRESSING_NEIGHBOR_NAMING: {
        "dhcp_missing_subnet",
        "dhcp_service_down",
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
        "receiver_resource_contention",
        "sender_resource_contention",
    },
    FailureDomain.TRAFFIC_QUEUEING_RESOURCE: {
        "incast_traffic_network_limitation",
        "p4_ecn_threshold_misconfiguration",
        "tcp_receive_window_limited",
    },
    FailureDomain.SECURITY: {
        "arp_cache_poisoning",
        "bgp_hijacking",
        "dhcp_spoofed_dns",
        "dhcp_spoofed_gateway",
        "dhcp_spoofed_subnet",
        "tcp_syn_flood_attack",
        "web_dos_attack",
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
        assert len(problems) == 75
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

    def test_link_packet_corruption_replaces_legacy_names(self) -> None:
        assert "link_packet_corruption" in list_avail_problem_names()
        assert "link_high_packet_corruption" not in list_avail_problem_names()
        assert "physical_link_corruption" not in list_avail_problem_names()
        assert get_problem_class("link_high_packet_corruption") is get_problem_class(
            "link_packet_corruption"
        )
        assert get_problem_class("physical_link_corruption") is get_problem_class(
            "link_packet_corruption"
        )

    def test_silent_egress_packet_loss_replaces_legacy_name(self) -> None:
        assert "silent_egress_packet_loss" in list_avail_problem_names()
        assert "faulty_egress_interface" not in list_avail_problem_names()
        assert get_problem_class("faulty_egress_interface") is get_problem_class(
            "silent_egress_packet_loss"
        )

    def test_link_down_is_undirected_link(self) -> None:
        env = _Env(("pc1:eth0", "router1:eth0"))
        resource = _resources(
            "link_down", {"host_name": "pc1", "intf_name": "eth0"}, env
        )
        assert resource.kind == "link"
        assert resource.id == "link/pc1:eth0--router1:eth0"

    def test_link_quality_is_link_even_on_lan(self) -> None:
        env = _Env(
            ("pc1:eth0", "pc2:eth0", "r1:eth0", "a:eth0", "b:eth0", "c:eth0", "d:eth0")
        )
        resource = _resources("link_packet_corruption", {"host_name": "pc1"}, env)
        assert resource.kind == "link"
        assert resource.id.startswith("link/")
        assert "pc1:eth0" in resource.name

    def test_device_forwarding_corruption_is_a_node(self) -> None:
        env = _Env(("spine_router_0_0:eth1", "leaf_router_0_0:eth0"))
        resource = _resources(
            "device_forwarding_packet_corruption",
            {"forwarding_device": "spine_router_0_0", "intf_name": "eth1", "seed": 7},
            env,
        )
        assert resource.id == "node/spine_router_0_0"

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
        from nika.problems.registry import get_problem_class

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
        from nika.problems.registry import get_problem_class

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
        assert gt.root_causes[0].resource.id == "link/pc1:eth0--r1:eth0"
        assert gt.failure_domain == "link_interface"

    def test_multi_root_cause(self) -> None:
        env = _Env(("pc1:eth0", "r1:eth0"))
        from nika.problems.registry import get_problem_class

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
                _piece("host_missing_ip", {"host_name": "pc1"}),
            ],
            failure_domain="multiple_faults",
        )
        assert len(gt.root_causes) == 2
        assert {item.fault_type for item in gt.root_causes} == {
            "link_down",
            "host_missing_ip",
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
        assert gt.root_causes[0].resource.id == "link/pc1:eth0--router1:eth1"


class RootCauseSchemaContractTest:
    """Ground-truth v3 schema contracts (canonical sort, healthy baseline)."""

    def test_submit_shape_resource_id(self) -> None:
        cause = RootCause(
            resource_id="link/pc1:eth0--router1:eth0", fault_type="link_down"
        )
        assert cause.resource is not None
        assert cause.resource.id == "link/pc1:eth0--router1:eth0"
        assert cause.pair_key() == ("link/pc1:eth0--router1:eth0", "link_down")

    def test_canonical_sort(self) -> None:
        items = [
            RootCause(resource=node_resource("b"), fault_type="host_missing_ip"),
            RootCause(resource=node_resource("a"), fault_type="host_missing_ip"),
        ]
        dumped = canonical_root_causes(items)
        assert dumped[0]["resource"] == {"kind": "node", "node": "a"}
        assert dumped[1]["resource"] == {"kind": "node", "node": "b"}

    def test_healthy_empty(self) -> None:
        gt = healthy_ground_truth()
        assert gt.is_anomaly is False
        assert gt.root_causes == []
        assert gt.failure_domain == ""

    def test_resource_roundtrip(self) -> None:
        original = interface_resource("leaf1", "e1-1")
        parsed = FaultResource.model_validate(original.model_dump())
        assert parsed.id == original.id

    def test_resource_from_id_contract(self) -> None:
        assert resource_from_id("node/pc1").id == "node/pc1"
        assert resource_from_id("interface/pc1/eth0").id == "interface/pc1/eth0"
        assert (
            resource_from_id("link/pc1:eth0--router1:eth0").id
            == "link/pc1:eth0--router1:eth0"
        )


class LinkInventoryHelpersTest:
    def test_canonical_link_name_sorts_tps(self) -> None:
        assert (
            canonical_link_name(("router1:eth0", "pc1:eth0"))
            == "pc1:eth0--router1:eth0"
        )

    def test_link_containing_endpoint_p2p(self) -> None:
        env = _Env(("pc1:eth0", "router1:eth0"))
        resource = link_containing_endpoint(env, "pc1", "eth0")
        assert resource == link_resource("pc1:eth0--router1:eth0")

    def test_link_containing_endpoint_missing(self) -> None:
        env = _Env(("pc1:eth0", "router1:eth0"))
        with pytest.raises(UnresolvedRootCauseError, match="No link contains"):
            link_containing_endpoint(env, "ghost", "eth0")

    def test_catalog_emits_links(self) -> None:
        env = _Env(("pc1:eth0", "router1:eth0"))
        ids = {item.id for item in catalog_resources(env)}
        assert "link/pc1:eth0--router1:eth0" in ids
        assert "interface/pc1/eth0" in ids
        assert "node/pc1" in ids
