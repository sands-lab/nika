"""Resolve inject parameters when generating benchmark YAML (offline only)."""

from __future__ import annotations

import hashlib
import random
from collections import defaultdict

from nika.net_env.net_env_pool import (
    get_net_env_instance,
    list_all_net_envs,
    scenario_backend,
)
from nika.problems.prob_pool import list_avail_problem_instances

DEFAULT_SEED = 42

_DEVICE_KEYS = (
    "host_name",
    "host_name_2",
    "attacker_device",
    "control_node",
    "node_name",
)


def _case_rng(seed: int, scenario: str, problem: str, topo_size: str) -> random.Random:
    key = f"{seed}|{scenario}|{problem}|{topo_size}".encode()
    digest = int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(), "big")
    return random.Random(digest)


def _choice(rng: random.Random, pool: list[str] | None, fallback: str) -> str:
    items = pool or []
    if not items:
        return fallback
    return rng.choice(items)


def _choice_distinct(
    rng: random.Random, pool: list[str] | None, fallback: str, *, n: int = 2
) -> list[str]:
    items = list(pool or [])
    if len(items) >= n:
        return rng.sample(items, n)
    if not items:
        return [fallback] * n
    if len(items) == 1:
        return [items[0], items[0]]
    return items[:n]


def _first(items: list[str] | None) -> str | None:
    return items[0] if items else None


def _parse_endpoint(endpoint: str) -> tuple[str, str]:
    device, _, intf = endpoint.partition(":")
    return device, intf or ""


def _device_interfaces(net_env) -> dict[str, list[str]]:
    mapping: dict[str, set[str]] = defaultdict(set)

    topo = net_env.get_topology()
    if topo:
        for link in topo:
            for endpoint in link:
                device, intf = _parse_endpoint(endpoint)
                if device and intf:
                    mapping[device].add(intf)
    else:
        spec = net_env.get_lab_spec()
        if spec is not None:
            for link in spec.links:
                for endpoint in link.endpoints:
                    device, intf = _parse_endpoint(endpoint)
                    if device and intf:
                        mapping[device].add(intf)

    return {device: sorted(intfs) for device, intfs in mapping.items()}


def _default_interface(backend: str) -> str:
    return "e1-1" if backend == "containerlab" else "eth0"


def _choice_interface(
    rng: random.Random,
    net_env,
    device: str,
    backend: str,
) -> str:
    ifaces = _device_interfaces(net_env).get(device) or []
    if ifaces:
        return rng.choice(ifaces)
    return _default_interface(backend)


def _dns_record_targets(net_env, rng: random.Random) -> tuple[str, str]:
    urls = getattr(net_env, "web_urls", None) or []
    if urls:
        url = rng.choice(urls)
        website = url.split(".")[0]
        if website.startswith("http://"):
            website = website[len("http://") :]
        domain = url.split(".")[1] if "." in url else "local"
        return website, domain
    web_pool = net_env.servers.get("web") or []
    web = _choice(rng, web_pool, "web0")
    if web:
        return web.replace("web_server_", "web"), "local"
    return "web0", "local"


def _mac_conflict_pair(net_env, rng: random.Random) -> tuple[str, str]:
    topo = net_env.get_topology()
    if topo:
        link = rng.choice(topo)
        device_a = link[0].split(":")[0]
        device_b = link[1].split(":")[0]
        return device_a, device_b
    hosts = net_env.hosts or []
    pair = _choice_distinct(rng, hosts, "pc1")
    return pair[0], pair[1]


def _flow_rule_loop_pair(net_env, rng: random.Random) -> tuple[str, str]:
    switches = net_env.ovs_switches or []
    pair = _choice_distinct(rng, switches, "leaf_1")
    return pair[0], pair[1]


def _all_device_names(net_env) -> set[str]:
    names: set[str] = (
        set(net_env.lab.machines.keys()) if net_env.lab is not None else set()
    )
    names.update(net_env.hosts or [])
    names.update(net_env.routers or [])
    names.update(net_env.bmv2_switches or [])
    names.update(net_env.ovs_switches or [])
    names.update(net_env.sdn_controllers or [])
    for bucket in (net_env.servers or {}).values():
        names.update(bucket)
    names.update(getattr(net_env, "kubernetes_nodes", []) or [])
    if net_env.lab is None:
        spec = net_env.get_lab_spec()
        if spec is not None:
            names.update(node.name for node in spec.nodes)
    return names


def _get_net_env_for_benchmark(scenario: str, topo_size: str = ""):
    kwargs: dict = {}
    if topo_size:
        kwargs["topo_size"] = topo_size
    kwargs["backend"] = scenario_backend(scenario)
    return get_net_env_instance(scenario, **kwargs)


def _load_inventory(net_env) -> None:
    if net_env.lab is not None:
        net_env.load_machines()
        return

    spec = net_env.get_lab_spec()
    if spec is None:
        raise ValueError(f"Cannot derive benchmark inventory for {net_env.name!r}.")

    net_env.bmv2_switches = []
    net_env.ovs_switches = []
    net_env.sdn_controllers = []
    net_env.hosts = []
    net_env.routers = []
    net_env.switches = []
    net_env.servers = defaultdict(list)

    for node in spec.nodes:
        name = node.name
        kind = node.kind.lower()
        image = node.image.lower()
        if any(key in name for key in ("client", "pc", "host")) or kind == "linux":
            net_env.hosts.append(name)
        elif any(key in kind for key in ("srl", "ceos", "router")) or any(
            key in image for key in ("srl", "ceos", "frr")
        ):
            net_env.routers.append(name)
        else:
            net_env.switches.append(name)

    net_env.hosts = sorted(net_env.hosts)
    net_env.routers = sorted(net_env.routers)
    net_env.switches = sorted(net_env.switches)


def _scenario_device_pools(scenario: str, net_env) -> dict[str, list[str]]:
    """Role-constrained device pools for scenario-specific labs."""
    hosts = net_env.hosts or []
    routers = net_env.routers or []
    k8s_nodes = getattr(net_env, "kubernetes_nodes", []) or []

    if scenario == "k8s_lab":
        client_pool = [h for h in hosts if "client" in h] or hosts
        router_pool = [r for r in routers if "leaf" in r] or routers
        controller_pool = [n for n in k8s_nodes if "controller" in n] or k8s_nodes
        return {
            "hosts": client_pool,
            "host1_pool": client_pool,
            "routers": router_pool,
            "web": client_pool,
            "attacker_pool": client_pool,
            "k8s_nodes": k8s_nodes,
            "k8s_controllers": controller_pool,
        }
    if scenario == "llmd_lab":
        client_pool = [h for h in hosts if "client" in h] or hosts
        controller_pool = [n for n in k8s_nodes if "controller" in n] or k8s_nodes
        return {
            "hosts": client_pool,
            "host1_pool": client_pool,
            "routers": controller_pool or client_pool,
            "web": client_pool,
            "attacker_pool": client_pool,
            "controllers": controller_pool,
            "k8s_nodes": k8s_nodes,
            "k8s_controllers": controller_pool,
        }
    if scenario == "min3clos":
        client_pool = [h for h in hosts if "client" in h] or hosts
        router_pool = [r for r in routers if "leaf" in r] or routers
        return {
            "hosts": client_pool,
            "host1_pool": client_pool,
            "routers": router_pool,
            "web": client_pool,
            "attacker_pool": client_pool,
        }
    return {}


def _pick_attacker(
    rng: random.Random,
    hosts: list[str],
    victim: str,
    fallback: str,
    *,
    pool: list[str] | None = None,
) -> str:
    candidates = [h for h in (pool or hosts) if h != victim]
    if not candidates:
        candidates = [h for h in hosts if h != victim]
    if not candidates:
        return fallback
    return rng.choice(candidates)


def resolve_inject_params(
    problem: str,
    scenario: str,
    topo_size: str = "",
    *,
    seed: int = DEFAULT_SEED,
) -> dict[str, str]:
    """Return inject params for one benchmark row."""
    rng = _case_rng(seed, scenario, problem, topo_size)
    net_env = _get_net_env_for_benchmark(scenario, topo_size)
    _load_inventory(net_env)
    backend = scenario_backend(scenario)

    hosts = net_env.hosts or []
    routers = net_env.routers or []
    servers = net_env.servers or {}
    bmv2 = net_env.bmv2_switches or []
    controllers = net_env.sdn_controllers or []

    pools = _scenario_device_pools(scenario, net_env)
    host_pool = pools.get("hosts") or hosts
    router_pool = pools.get("routers") or routers

    host0 = _choice(rng, host_pool, _first(hosts) or "pc1")
    router0 = _choice(rng, router_pool, _first(routers) or host0)
    dns0 = _choice(rng, servers.get("dns"), host0)
    dhcp0 = _choice(rng, servers.get("dhcp"), dns0)
    web0 = _choice(rng, pools.get("web") or servers.get("web"), host0)
    vpn0 = _choice(rng, servers.get("vpn"), host0)
    lb0 = _choice(rng, servers.get("load_balancer"), web0)

    params: dict[str, str] = {}

    if problem in {
        "link_down",
        "link_flap",
        "link_detach",
        "link_fragmentation_disabled",
        "link_high_packet_corruption",
        "link_bandwidth_throttling",
        "host_missing_ip",
        "host_incorrect_ip",
        "host_incorrect_gateway",
        "host_incorrect_netmask",
        "host_incorrect_dns",
        "host_crash",
        "arp_cache_poisoning",
        "receiver_resource_contention",
    }:
        if scenario == "min3clos" and problem.startswith("link_"):
            params["host_name"] = router0
            params["intf_name"] = _choice_interface(rng, net_env, router0, backend)
        else:
            params["host_name"] = host0
            if problem.startswith("link_"):
                params["intf_name"] = _choice_interface(rng, net_env, host0, backend)
            elif problem == "host_missing_ip":
                params["intf_name"] = _choice_interface(rng, net_env, host0, backend)
        if problem == "link_flap":
            params["down_time"] = "30"
            params["up_time"] = "30"
        if problem == "link_fragmentation_disabled":
            params["mtu"] = "100"
        if problem == "host_incorrect_netmask":
            params["netmask_prefix"] = "8"
        if problem == "link_bandwidth_throttling":
            params["rate"] = "30kbit"
            params["burst"] = "64kb"
            params["limit"] = "500kb"
        if problem == "link_high_packet_corruption":
            params["corruption_percentage"] = "60"
        if problem == "receiver_resource_contention":
            params["duration"] = "600"

    elif problem == "host_ip_conflict":
        pair = _choice_distinct(rng, host_pool, host0)
        params["host_name"] = pair[0]
        params["host_name_2"] = pair[1]

    elif problem == "dns_record_error":
        website, domain = _dns_record_targets(net_env, rng)
        params["host_name"] = dns0
        params["target_website"] = website
        params["target_domain"] = domain

    elif problem in {"dns_service_down"}:
        params["host_name"] = dns0

    elif problem in {"dhcp_service_down", "dhcp_missing_subnet"}:
        client = _choice(
            rng,
            [h for h in host_pool if h != dhcp0] or host_pool,
            host0,
        )
        params["host_name"] = dhcp0
        params["host_name_2"] = client

    elif problem in {"dhcp_spoofed_gateway", "dhcp_spoofed_dns", "dhcp_spoofed_subnet"}:
        client = _choice(
            rng,
            [h for h in host_pool if h != dhcp0] or host_pool,
            host0,
        )
        params["host_name"] = dhcp0
        params["host_name_2"] = client

    elif problem == "host_vpn_membership_missing":
        vpn_server = vpn0
        vpn_peer_pool: list[str] = []
        devices = _all_device_names(net_env)
        if "pc1" in devices:
            vpn_peer_pool.append("pc1")
        vpn_peer_pool.extend(servers.get("web") or [])
        if not vpn_peer_pool:
            vpn_peer_pool = list(host_pool)
        params["host_name"] = _choice(rng, vpn_peer_pool, host0)
        params["host_name_2"] = vpn_server

    elif problem in {
        "bgp_acl_block",
        "bgp_asn_misconfig",
        "bgp_missing_route_advertisement",
        "host_static_blackhole",
        "bgp_blackhole_route_leak",
        "bgp_hijacking",
        "ospf_acl_block",
        "ospf_area_misconfiguration",
        "ospf_neighbor_missing",
        "frr_service_down",
    }:
        params["host_name"] = router0

    elif problem in {"arp_acl_block", "icmp_acl_block", "http_acl_block"}:
        params["host_name"] = host0

    elif problem == "dns_port_blocked":
        params["host_name"] = dns0

    elif problem == "mac_address_conflict":
        a, b = _mac_conflict_pair(net_env, rng)
        params["host_name"] = a
        params["host_name_2"] = b

    elif problem in {
        "p4_header_definition_error",
        "p4_compilation_error_parser_state",
        "p4_table_entry_missing",
        "p4_table_entry_misconfig",
        "p4_aggressive_detection_thresholds",
        "bmv2_switch_down",
        "mpls_label_limit_exceeded",
    }:
        params["host_name"] = _choice(rng, bmv2, host0)

    elif problem in {
        "sdn_controller_crash",
        "southbound_port_block",
        "southbound_port_mismatch",
    }:
        controller_pool = pools.get("controllers") or controllers
        params["host_name"] = _choice(rng, controller_pool, host0)
        if problem == "southbound_port_block":
            params["southbound_port"] = "6633"
        if problem == "southbound_port_mismatch":
            params["mismatched_port"] = "6653"
            params["original_port"] = "6633"

    elif problem == "flow_rule_shadowing":
        params["host_name"] = _choice(rng, net_env.ovs_switches, host0)

    elif problem == "flow_rule_loop":
        a, b = _flow_rule_loop_pair(net_env, rng)
        params["host_name"] = a
        params["host_name_2"] = b

    elif problem == "web_dos_attack":
        if scenario == "llmd_lab":
            controller_pool = pools.get("controllers") or []
            params["host_name"] = _choice(rng, controller_pool, host0)
            params["attacker_device"] = _pick_attacker(
                rng,
                hosts,
                params["host_name"],
                host0,
                pool=pools.get("attacker_pool"),
            )
        else:
            params["host_name"] = web0
            params["attacker_device"] = _pick_attacker(rng, hosts, web0, host0)

    elif problem == "dns_lookup_latency":
        dns_target = dns0 if dns0 in _all_device_names(net_env) else host0
        params["host_name"] = dns_target
        params["intf_name"] = _choice_interface(rng, net_env, dns_target, backend)
        params["delay_ms"] = "1000"

    elif problem == "incast_traffic_network_limitation":
        web_pool = servers.get("web") or []
        params["host_name"] = web0 if web0 in web_pool else host0
        params["rate"] = "1mbit"
        params["burst"] = "500kb"
        params["limit"] = "500kb"
        params["delay_ms"] = "20"

    elif problem in {"sender_resource_contention", "sender_application_delay"}:
        web_pool = servers.get("web") or []
        params["host_name"] = web0 if web0 in web_pool else host0
        if problem == "sender_resource_contention":
            params["duration"] = "600"

    elif problem in {"k8s_clusterip_routing_broken", "k8s_worker_apiserver_partition"}:
        k8s_nodes = pools.get("k8s_nodes") or []
        control = _first(pools.get("k8s_controllers")) or _first(k8s_nodes) or host0
        workers = sorted(node for node in k8s_nodes if node != control)
        params["control_node"] = control
        params["node_name"] = _choice(rng, workers, control)

    elif problem == "k8s_coredns_isolated":
        k8s_nodes = pools.get("k8s_nodes") or []
        control = _first(pools.get("k8s_controllers")) or _first(k8s_nodes) or host0
        # node_name is intentionally left unset: the fault resolves the nodes
        # actually hosting CoreDNS at inject time, which is where isolating it
        # takes the whole cluster's name resolution down.
        params["control_node"] = control

    elif problem == "load_balancer_overload":
        params["host_name"] = lb0
        params["duration"] = "300"

    else:
        params["host_name"] = host0

    return params


def validate_benchmark_case(
    scenario: str,
    problem: str,
    inject: dict[str, str],
    topo_size: str = "",
) -> None:
    """Raise ValueError if a benchmark row is inconsistent with tags or topology."""
    net_envs = list_all_net_envs()
    problems = list_avail_problem_instances()
    if scenario not in net_envs:
        raise ValueError(f"Unknown scenario {scenario!r}")
    if problem not in problems:
        raise ValueError(f"Unknown problem {problem!r}")

    problem_tags = set(problems[problem].TAGS)
    scenario_tags = set(net_envs[scenario].TAGS)
    if not problem_tags.issubset(scenario_tags):
        raise ValueError(
            f"Tag mismatch for {problem} on {scenario}: "
            f"problem tags {sorted(problem_tags)} not subset of scenario tags {sorted(scenario_tags)}"
        )

    net_env = _get_net_env_for_benchmark(scenario, topo_size)
    _load_inventory(net_env)
    devices = _all_device_names(net_env)
    ifaces_by_device = _device_interfaces(net_env)

    for key in _DEVICE_KEYS:
        value = inject.get(key)
        if value and value not in devices:
            raise ValueError(
                f"Inject device {key}={value!r} not in {scenario} topology "
                f"(topo_size={topo_size!r}); known devices: {sorted(devices)}"
            )

    host_name = inject.get("host_name")
    intf_name = inject.get("intf_name")
    if host_name and intf_name:
        device_ifaces = ifaces_by_device.get(host_name) or []
        if device_ifaces and intf_name not in device_ifaces:
            raise ValueError(
                f"Inject interface {intf_name!r} not on {host_name!r} in {scenario} "
                f"(topo_size={topo_size!r}); known interfaces: {device_ifaces}"
            )

    host_a = inject.get("host_name")
    host_b = inject.get("host_name_2")
    if host_a and host_b and host_a == host_b:
        hosts = net_env.hosts or []
        if problem == "host_ip_conflict" and len(hosts) >= 2:
            raise ValueError(
                f"Inject devices host_name and host_name_2 must differ for {problem} "
                f"on {scenario} when multiple hosts exist"
            )
        if problem == "flow_rule_loop" and len(net_env.ovs_switches or []) >= 2:
            raise ValueError(
                f"Inject devices host_name and host_name_2 must differ for {problem} "
                f"on {scenario} when multiple OVS switches exist"
            )

    if problem == "dns_record_error":
        website = inject.get("target_website", "")
        domain = inject.get("target_domain", "")
        urls = getattr(net_env, "web_urls", None) or []
        if urls:
            matched = any(
                website in url and (not domain or domain in url) for url in urls
            )
            if not matched:
                raise ValueError(
                    f"DNS record targets {website}.{domain} not found in web_urls for {scenario}: {urls}"
                )
