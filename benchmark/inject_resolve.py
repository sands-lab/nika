"""Resolve inject parameters when generating benchmark YAML (offline only)."""

from __future__ import annotations

import hashlib
import random
from collections import defaultdict

from nika.net_env.net_env_pool import get_net_env_instance
from nika.problems.registry import list_avail_problem_instances

DEFAULT_SEED = 42

_DEVICE_KEYS = (
    "host_name",
    "host_name_2",
    "attacker_device",
    "control_node",
    "node_name",
    "symptom_host",
    "forwarding_device",
)


def _case_rng(
    seed: int,
    scenario: str,
    problem: str,
    topo_size: str,
    isp_key: str = "",
) -> random.Random:
    key = f"{seed}|{scenario}|{problem}|{topo_size}"
    if isp_key:
        key = f"{key}|{isp_key}"
    digest = int.from_bytes(
        hashlib.blake2b(key.encode(), digest_size=8).digest(), "big"
    )
    return random.Random(digest)


def _isp_rng_key(isp_options: dict[str, str] | None) -> str:
    if not isp_options:
        return ""
    return (
        f"{isp_options.get('topo', '')}|"
        f"{isp_options.get('igp', '')}|"
        f"{isp_options.get('bgp_mode', '')}|"
        f"{isp_options.get('rpki', False)}"
    )


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


def _enterprise_branch_corp_hosts(hosts: list[str]) -> list[str]:
    """CORP/SERVER hosts on the overlay business path (exclude guest/iot)."""
    corp = [
        h for h in hosts if "_corp_pc" in h or h.endswith("_srv") or h.endswith("_srv2")
    ]
    if corp:
        return corp
    return [h for h in hosts if "_guest_pc" not in h and "_iot_pc" not in h]


def _enterprise_branch_edge_routers(routers: list[str]) -> list[str]:
    return [r for r in routers if r.endswith("_edge")] or list(routers)


def _routers_with_bgp_network(routers: list[str], *, scenario: str = "") -> list[str]:
    """Routers that originate BGP ``network`` statements in Clos-style labs.

    Spines (and ``dc_clos`` super-spines without a client subnet) only peer and
    have no ``network`` lines, so commenting those out is a no-op. Prefer leaves;
    fall back to the full pool when the topology does not use leaf role names.
    """
    if scenario == "enterprise_branch":
        return _enterprise_branch_edge_routers(routers)
    advertisers = [r for r in routers if "leaf" in r]
    return advertisers or list(routers)


def _routers_with_victim_hosts(routers: list[str], *, scenario: str = "") -> list[str]:
    """Routers that have end hosts for ``resolve_victim_host()``.

    Clos spines / super-spines only connect to other routers, so blackhole /
    hijack / leak injectors that call ``resolve_victim_host`` fail on them.
    Prefer leaf routers when the topology uses that naming.
    """
    return _routers_with_bgp_network(routers, scenario=scenario)


# Legacy host-level WireGuard peer names are unused; Site Edge VPN uses
# wireguard_peer_key_misconfiguration / wireguard_allowed_ips_misconfiguration.


def _single_path_hq_wg_targets(topo_size: str) -> list[tuple[str, str]]:
    """Eligible (branch_edge, wg_iface) pairs for Site Edge peer-key faults.

    Alias of primary HQ targets: every branch is multi-path; peer-key inject
    breaks all WG ifaces on the selected edge.
    """
    return _primary_hq_wg_targets(topo_size)


def _primary_hq_wg_targets(topo_size: str) -> list[tuple[str, str]]:
    """Eligible (branch_edge, wg_iface) pairs for AllowedIPs faults."""
    from nika.net_env.enterprise_branch.topology import (
        primary_hq_peer_targets,
    )

    size = topo_size if topo_size in {"s", "m", "l"} else "s"
    return primary_hq_peer_targets(size)  # type: ignore[arg-type]


def _remote_prefixes_for_spoke(topo_size: str, spoke: str) -> list[str]:
    from nika.net_env.enterprise_branch.topology import (
        remote_advertised_prefixes_for_spoke,
    )

    size = topo_size if topo_size in {"s", "m", "l"} else "s"
    return remote_advertised_prefixes_for_spoke(size, spoke)  # type: ignore[arg-type]


def _dscp_remark_targets(topo_size: str):
    from nika.net_env.enterprise_branch.topology import (
        dscp_remark_inject_targets,
    )

    size = topo_size if topo_size in {"s", "m", "l"} else "s"
    return dscp_remark_inject_targets(size)  # type: ignore[arg-type]


def _prefer_hq_server_prefix(prefixes: list[str]) -> str | None:
    """Prefer HQ SERVER (10.0.20.0/24), then HQ CORP, else first remote prefix."""
    if not prefixes:
        return None
    for preferred in ("10.0.20.0/24", "10.0.10.0/24"):
        if preferred in prefixes:
            return preferred
    return prefixes[0]


_WG_TUNNEL_IFACE_PROBLEMS = frozenset(
    {
        "wireguard_peer_key_misconfiguration",
        "wireguard_allowed_ips_misconfiguration",
        "vrf_dscp_remarking",
    }
)


_VICTIM_HOST_PROBLEMS = frozenset(
    {
        "host_static_blackhole",
        "bgp_blackhole_route_leak",
        "bgp_hijacking",
    }
)


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


def _first_iface(ifaces: list[str], fallback: str = "eth0") -> str:
    if not ifaces:
        return fallback
    return sorted(ifaces, key=lambda name: (len(name), name))[0]


def _resolve_path_mtu_target(
    scenario: str,
    net_env,
    rng: random.Random,
    routers: list[str],
    backend: str,
) -> dict[str, str]:
    """Pick an intermediate L3 egress for real path-MTU reduction."""
    ifaces_by_device = _device_interfaces(net_env)
    params: dict[str, str] = {"mtu": "500"}

    if scenario == "dc_clos":
        # Lower MTU on the leaf host-facing egress that serves the default
        # webserver probe target (unique hop; avoids SS ECMP bypass).
        from nika.problems.rca.inventory import (
            iter_link_termination_points,
            parse_endpoint,
        )

        web_hosts = list((getattr(net_env, "servers", None) or {}).get("web") or [])
        host = None
        intf = None
        for web in web_hosts:
            needle_prefix = f"{web}:"
            for _key, tps in iter_link_termination_points(net_env):
                endpoints = [str(ep) for ep in tps]
                web_ep = next(
                    (ep for ep in endpoints if ep.startswith(needle_prefix)), None
                )
                if web_ep is None or len(endpoints) != 2:
                    continue
                other = endpoints[0] if endpoints[1] == web_ep else endpoints[1]
                peer_host, peer_intf = parse_endpoint(other)
                if peer_host.startswith("leaf_router_"):
                    host = peer_host
                    intf = peer_intf
                    break
            if host is not None:
                break
        if host is None:
            candidates = [n for n in routers if str(n).startswith("leaf_router_")]
            host = (
                "leaf_router_0_1"
                if "leaf_router_0_1" in candidates
                else _choice(rng, candidates, "leaf_router_0_0")
            )
            host_ifaces = ifaces_by_device.get(host) or ["eth0"]
            ordered = sorted(host_ifaces, key=lambda name: (len(name), name))
            intf = ordered[-1] if ordered else "eth0"
        params.update(host_name=host, intf_name=intf or "eth0")
        return params

    if scenario == "campus_lan":
        candidates = [
            n for n in routers if "router_core" in n or "router_dist" in n
        ] or list(routers)
        host = _choice(
            rng, candidates, candidates[0] if candidates else "router_core_1"
        )
        params.update(
            host_name=host,
            intf_name=_first_iface(ifaces_by_device.get(host) or [], "eth0"),
        )
        return params

    if scenario == "enterprise_branch":
        host = (
            "br1_edge"
            if "br1_edge" in routers
            else _choice(rng, list(routers), "br1_edge")
        )
        host_ifaces = ifaces_by_device.get(host) or []
        if "eth2" in host_ifaces:
            intf = "eth2"
        elif host_ifaces:
            intf = host_ifaces[-1]
        else:
            intf = "eth2"
        params.update(host_name=host, intf_name=intf)
        return params

    if scenario == "k8s_lab":
        candidates = [
            n for n in routers if "leaf" in str(n) or "spine" in str(n)
        ] or list(routers)
        host = _choice(rng, candidates, candidates[0] if candidates else "leaf_0_0")
        params.update(
            host_name=host,
            intf_name=_first_iface(ifaces_by_device.get(host) or [], "eth0"),
        )
        return params

    if scenario == "isp":
        from nika.net_env.isp.inject_targets import isp_inject_params

        inventory = getattr(net_env, "inventory", None)
        if not isinstance(inventory, dict):
            inventory = {}
        link_params = isp_inject_params(
            "link_capacity_bottleneck", inventory, inventory.get("bgp")
        )
        params.update(
            host_name=str(link_params["host_name"]),
            intf_name=str(link_params["intf_name"]),
        )
        return params

    host = _choice(rng, list(routers), "router1")
    params.update(
        host_name=host,
        intf_name=_choice_interface(rng, net_env, host, backend),
    )
    return params


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
    """Prefer two distinct endpoint hosts so L2 MAC conflict is observable."""
    hosts = list(net_env.hosts or [])
    servers = net_env.servers or {}
    for bucket in servers.values():
        for name in bucket or []:
            if name not in hosts:
                hosts.append(name)
    if len(hosts) >= 2:
        pair = _choice_distinct(rng, hosts, hosts[0])
        return pair[0], pair[1]
    topo = net_env.get_topology()
    if topo:
        link = rng.choice(topo)
        device_a = link[0].split(":")[0]
        device_b = link[1].split(":")[0]
        return device_a, device_b
    pair = _choice_distinct(rng, hosts, "pc1")
    return pair[0], pair[1]


def _flow_rule_loop_pair(net_env, rng: random.Random) -> tuple[str, str, str, str]:
    """Return (switch_a, switch_b, port_a, port_b) preferring an adjacent fabric link."""
    model = getattr(net_env, "model", None)
    if model is not None and model.leaf_spine_links:
        leaf, spine = rng.choice(model.leaf_spine_links)
        p0 = model.port_to_peer(leaf, spine)
        p1 = model.port_to_peer(spine, leaf)
        if p0 is not None and p1 is not None:
            return leaf, spine, p0.name, p1.name
    switches = net_env.ovs_switches or []
    pair = _choice_distinct(rng, switches, "leaf_1")
    return pair[0], pair[1], "eth0", "eth0"


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


def _get_net_env_for_benchmark(
    scenario: str,
    topo_size: str = "",
    *,
    isp_options: dict[str, str] | None = None,
):
    kwargs: dict = {}
    if topo_size:
        kwargs["topo_size"] = topo_size
    if isp_options:
        kwargs.update(isp_options)
    from nika.net_env.isp.profiles import DEFAULT_BACKEND_FOR_ISP
    from nika.net_env.net_env_pool import resolve_scenario_backend

    kwargs["backend"] = resolve_scenario_backend(
        scenario, default_when_ambiguous=DEFAULT_BACKEND_FOR_ISP
    )
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
    if scenario == "sdn_l3_clos":
        client_pool = [h for h in hosts if "client" in h] or hosts
        web_pool = list((net_env.servers or {}).get("web") or [])
        controllers = net_env.sdn_controllers or ["onos"]
        return {
            "hosts": client_pool,
            "host1_pool": client_pool,
            "web": web_pool or client_pool,
            "attacker_pool": client_pool,
            "controllers": controllers,
        }
    if scenario in {"p4_dc_fabric", "p4_dc_gateway"}:
        client_pool = [h for h in hosts if "client" in h] or hosts
        web_pool = list((net_env.servers or {}).get("web") or [])
        return {
            "hosts": client_pool,
            "host1_pool": client_pool,
            "web": web_pool or client_pool,
            "attacker_pool": client_pool,
        }
    if scenario == "enterprise_branch":
        corp_pool = _enterprise_branch_corp_hosts(hosts)
        edge_pool = _enterprise_branch_edge_routers(routers)
        hq_web = [h for h in corp_pool if h.startswith("hq_")] or corp_pool
        br_attacker = [h for h in corp_pool if h.startswith("br1_")] or corp_pool
        return {
            "hosts": corp_pool,
            "host1_pool": corp_pool,
            "routers": edge_pool,
            "edges": edge_pool,
            "web": hq_web,
            "attacker_pool": br_attacker,
        }
    if scenario == "campus_lan":
        pc_pool = [h for h in hosts if h.startswith("pc_")] or hosts
        return {
            "hosts": pc_pool,
            "host1_pool": pc_pool,
            "web": pc_pool,
            "attacker_pool": pc_pool,
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
    isp_options: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return inject params for one benchmark row."""
    rng = _case_rng(
        seed,
        scenario,
        problem,
        topo_size,
        _isp_rng_key(isp_options),
    )
    net_env = _get_net_env_for_benchmark(scenario, topo_size, isp_options=isp_options)
    _load_inventory(net_env)
    from nika.net_env.isp.profiles import DEFAULT_BACKEND_FOR_ISP
    from nika.net_env.net_env_pool import resolve_scenario_backend

    backend = resolve_scenario_backend(
        scenario, default_when_ambiguous=DEFAULT_BACKEND_FOR_ISP
    )

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
    lb0 = _choice(rng, servers.get("load_balancer"), web0)

    params: dict[str, str] = {}

    if problem == "device_forwarding_packet_corruption":
        forwarding = list(routers) + list(net_env.switches or [])
        if not forwarding:
            raise ValueError(
                f"device_forwarding_packet_corruption requires a forwarding device in {scenario}"
            )
        if scenario == "dc_clos":
            candidates = [name for name in routers if name.startswith("spine_router_")]
        elif scenario == "sdn_l3_clos":
            candidates = [name for name in forwarding if name.startswith("spine_")]
        else:
            candidates = forwarding
        target = _choice(rng, candidates, forwarding[0])
        interfaces = _device_interfaces(net_env).get(target) or []
        params.update(
            forwarding_device=target,
            intf_name=interfaces[-1] if interfaces else "eth0",
            seed=str(seed),
        )

    elif scenario == "p4_dc_gateway" and problem in {
        "lb_connection_state_exhaustion",
        "lb_pending_connection_update_race",
        "icmp_frag_needed_filter_misconfiguration",
    }:
        params["host_name"] = "gateway_1"
        if problem == "lb_connection_state_exhaustion":
            params.update(capacity="256", syn_timeout_sec="10", seed=str(seed))
        elif problem == "lb_pending_connection_update_race":
            params.update(learning_delay_ms="5", seed=str(seed))

    elif problem == "icmp_frag_needed_filter_misconfiguration":
        # Linux / FRR path: drop Frag Needed on an intermediate router.
        mtu_target = _resolve_path_mtu_target(
            scenario, net_env, rng, list(router_pool), backend
        )
        params["host_name"] = mtu_target["host_name"]

    elif problem == "mtu_mismatch":
        params.update(
            _resolve_path_mtu_target(scenario, net_env, rng, list(router_pool), backend)
        )

    elif scenario == "p4_dc_gateway" and problem in {
        "p4_tcam_entry_corruption",
        "silent_egress_packet_loss",
        "p4_ecn_threshold_misconfiguration",
        "tcp_syn_flood_attack",
        "int_insufficient_mtu_headroom",
    }:
        model = net_env.model
        service = rng.choice(model.services)
        if problem == "tcp_syn_flood_attack":
            params.update(
                attacker_device=rng.choice(model.clients).name,
                target_ip=service.ip,
                target_port="80",
                rate_pps="100",
                duration="60",
                flows="40",
                seed=str(seed),
            )
        elif problem == "p4_tcam_entry_corruption":
            target = rng.choice(model.gateways + model.spines)
            control = (
                next(
                    client.name
                    for client in model.clients
                    if client.name != model.clients[0].name
                )
                if len(model.clients) > 1
                else model.clients[0].name
            )
            params.update(
                host_name=target, target_ip=service.ip, control_source=control
            )
        else:
            if problem == "int_insufficient_mtu_headroom":
                target = rng.choice(model.gateways)
                peer = rng.choice(model.spines)
            else:
                target = rng.choice(model.gateways + model.spines)
                candidates = [
                    port
                    for port in model.ports[target]
                    if port.role in {"spine", "leaf"}
                ]
                peer = rng.choice(candidates).peer
            port = model.port_to_peer(target, peer)
            assert port is not None
            params.update(
                host_name=target,
                intf_name=port.name,
                bmv2_port=str(port.bmv2_port),
            )
            if problem == "silent_egress_packet_loss":
                params.update(loss_basis_points="200", seed=str(seed))
            elif problem == "p4_ecn_threshold_misconfiguration":
                params["threshold"] = "1024"
            else:
                params["int_mtu"] = "1480"

    elif scenario == "isp" and problem in {
        "link_down",
        "link_flap",
        "link_detach",
        "link_capacity_bottleneck",
        "link_packet_corruption",
    }:
        from nika.net_env.isp.inject_targets import (
            isp_inject_params,
            isp_link_symptom_targets,
        )

        inventory = getattr(net_env, "inventory", None)
        if not isinstance(inventory, dict):
            inventory = {}
        bgp_inv = inventory.get("bgp")
        params.update(isp_inject_params(problem, inventory, bgp_inv))
        device = params.get("host_name")
        iface = params.get("intf_name")
        if device and iface:
            params.update(isp_link_symptom_targets(inventory, device, iface))
        if problem == "link_flap":
            params["down_time"] = "5"
            params["up_time"] = "5"
        elif problem == "link_capacity_bottleneck":
            params["rate"] = "10kbit"
            params["burst"] = "64kb"
            params["limit"] = "500kb"
        elif problem == "link_packet_corruption":
            params["corruption_percentage"] = "80"

    elif problem in {
        "link_down",
        "link_flap",
        "link_detach",
        "link_packet_corruption",
        "link_capacity_bottleneck",
        "host_missing_ip",
        "host_incorrect_ip",
        "host_incorrect_gateway",
        "host_incorrect_netmask",
        "host_incorrect_dns",
        "arp_cache_poisoning",
        "receiver_resource_contention",
    }:
        if scenario in {"sdn_l3_clos", "p4_dc_fabric"} and (
            problem.startswith("link_")
        ):
            # Prefer a leaf–spine fabric link so Clos path/ECMP failures are exercised.
            # Packet corruption uses the last iface; on a switch that is OOB, so pin
            # corruption to a client access link.
            if scenario == "p4_dc_fabric" and problem == "link_packet_corruption":
                params["host_name"] = host0
                params["intf_name"] = "eth0"
            else:
                model = getattr(net_env, "model", None)
                if model is not None and getattr(model, "leaf_spine_links", None):
                    leaf, spine = rng.choice(model.leaf_spine_links)
                    port = model.port_to_peer(leaf, spine)
                    params["host_name"] = leaf
                    params["intf_name"] = port.name if port is not None else "eth2"
                else:
                    switches = net_env.ovs_switches or net_env.bmv2_switches or []
                    leaf = _choice(
                        rng,
                        [n for n in switches if str(n).startswith("leaf_")] or switches,
                        "leaf_1",
                    )
                    params["host_name"] = leaf
                    params["intf_name"] = _choice_interface(rng, net_env, leaf, backend)
        elif scenario == "min3clos" and (problem.startswith("link_")):
            params["host_name"] = router0
            params["intf_name"] = _choice_interface(rng, net_env, router0, backend)
        elif scenario == "enterprise_branch":
            corp_src = (
                "br1_corp_pc"
                if "br1_corp_pc" in host_pool
                else _first(host_pool) or host0
            )
            if problem.startswith("link_"):
                params["host_name"] = corp_src
                params["intf_name"] = "eth0"
            else:
                params["host_name"] = corp_src
                if problem == "host_missing_ip":
                    params["intf_name"] = _choice_interface(
                        rng, net_env, corp_src, backend
                    )
        else:
            if problem == "arp_cache_poisoning" and scenario in {
                "p4_dc_fabric",
                "sdn_l3_clos",
                "min3clos",
            }:
                params["host_name"] = (
                    "client_1_1" if "client_1_1" in host_pool else host0
                )
            else:
                params["host_name"] = host0
            if problem.startswith("link_"):
                params["intf_name"] = _choice_interface(rng, net_env, host0, backend)
            elif problem == "host_missing_ip":
                params["intf_name"] = _choice_interface(rng, net_env, host0, backend)
        if problem == "link_flap":
            params["down_time"] = "5" if scenario == "enterprise_branch" else "30"
            params["up_time"] = "5" if scenario == "enterprise_branch" else "30"
        if problem == "host_incorrect_netmask":
            params["netmask_prefix"] = "8"
        if problem == "link_capacity_bottleneck":
            params["rate"] = (
                "10kbit"
                if scenario in {"enterprise_branch", "sdn_l3_clos", "p4_dc_fabric"}
                else "30kbit"
            )
            params["burst"] = "64kb"
            params["limit"] = "500kb"
        if problem == "link_packet_corruption":
            params["corruption_percentage"] = (
                "80"
                if scenario in {"sdn_l3_clos", "p4_dc_fabric", "enterprise_branch"}
                else "60"
            )
        if problem == "receiver_resource_contention":
            params["duration"] = "600"

    elif scenario == "enterprise_branch" and problem in {
        "snat_port_pool_exhaustion",
        "nat_mapping_removed_without_drain",
    }:
        edge = "br1_edge"
        if problem == "snat_port_pool_exhaustion":
            params.update(
                host_name=edge,
                source_prefix="10.1.40.0/24",
                public_ip="198.18.1.10",
                port_start="40000",
                port_end="40063",
            )
        else:
            params.update(
                host_name=edge,
                source_prefix="10.1.40.0/24",
                nat_ip_a="198.18.1.10",
                nat_ip_b="198.18.1.11",
                wan_interface="eth2",
            )

    elif problem == "host_ip_conflict":
        conflict_pool = list(host_pool)
        for bucket in (servers.get("web") or [], servers.get("dns") or []):
            for name in bucket:
                if name not in conflict_pool:
                    conflict_pool.append(name)
        if len(conflict_pool) < 2:
            # Fall back to all declared hosts + web servers from inventory.
            conflict_pool = list(
                dict.fromkeys(
                    list(hosts)
                    + list(servers.get("web") or [])
                    + list(servers.get("dns") or [])
                )
            )
        pair = _choice_distinct(rng, conflict_pool, host0)
        if pair[0] == pair[1] and len(conflict_pool) >= 2:
            pair = [conflict_pool[0], conflict_pool[1]]
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
        if scenario == "campus_lan" and "pc_1_1_1_1" in host_pool:
            client = "pc_1_1_1_1"
        params["host_name"] = dhcp0
        params["host_name_2"] = client
        if scenario == "campus_lan" and problem == "dhcp_missing_subnet":
            params["subnet"] = "10.1.1.0"

    elif problem in {"dhcp_spoofed_gateway", "dhcp_spoofed_dns", "dhcp_spoofed_subnet"}:
        client = _choice(
            rng,
            [h for h in host_pool if h != dhcp0] or host_pool,
            host0,
        )
        if scenario == "campus_lan" and "pc_1_1_1_1" in host_pool:
            client = "pc_1_1_1_1"
        params["host_name"] = dhcp0
        params["host_name_2"] = client
        if scenario == "campus_lan" and problem == "dhcp_spoofed_subnet":
            params["subnet"] = "10.1.1.0"

    elif problem == "wireguard_peer_key_misconfiguration":
        targets = _primary_hq_wg_targets(topo_size)
        if not targets:
            raise ValueError(
                f"No primary HQ WireGuard peers for enterprise_branch topo_size={topo_size!r}"
            )
        edge, iface = rng.choice(targets)
        params["host_name"] = edge
        params["intf_name"] = iface

    elif problem == "wireguard_allowed_ips_misconfiguration":
        targets = _primary_hq_wg_targets(topo_size)
        if not targets:
            raise ValueError(
                f"No primary HQ WireGuard peers for enterprise_branch topo_size={topo_size!r}"
            )
        edge, iface = rng.choice(targets)
        spoke = edge[: -len("_edge")] if edge.endswith("_edge") else edge
        remotes = _remote_prefixes_for_spoke(topo_size, spoke)
        target_prefix = _prefer_hq_server_prefix(remotes)
        if not target_prefix:
            raise ValueError(
                f"No remote advertised prefixes for spoke {spoke!r} (topo_size={topo_size!r})"
            )
        params["host_name"] = edge
        params["intf_name"] = iface
        params["target_prefix"] = target_prefix

    elif problem == "vrf_dscp_remarking":
        targets = _dscp_remark_targets(topo_size)
        if not targets:
            raise ValueError(
                f"No DSCP remark inject targets for enterprise_branch topo_size={topo_size!r}"
            )
        target = rng.choice(targets)
        params["host_name"] = target.edge
        params["intf_name"] = target.intf_name
        params["src_host"] = target.src_host
        params["dst_host"] = target.dst_host
        params["direction"] = "lan_to_overlay"
        params["corp_prefix"] = target.corp_prefix

    elif problem == "bgp_missing_route_advertisement":
        advertise_pool = _routers_with_bgp_network(router_pool, scenario=scenario)
        params["host_name"] = _choice(
            rng, advertise_pool, _first(advertise_pool) or router0
        )

    elif problem in _VICTIM_HOST_PROBLEMS:
        if scenario == "isp":
            from nika.net_env.isp.inject_targets import isp_inject_params

            inventory = getattr(net_env, "inventory", None)
            if not isinstance(inventory, dict):
                inventory = {}
            bgp_inv = inventory.get("bgp")
            params.update(
                isp_inject_params(
                    problem,
                    inventory,
                    bgp_inv if isinstance(bgp_inv, dict) else None,
                )
            )
        elif scenario == "enterprise_branch" and problem == "host_static_blackhole":
            victim_pool = _routers_with_victim_hosts(router_pool, scenario=scenario)
            params["host_name"] = (
                "hq_edge"
                if "hq_edge" in victim_pool
                else (
                    "br1_edge"
                    if "br1_edge" in victim_pool
                    else _first(victim_pool) or router0
                )
            )
        else:
            victim_pool = _routers_with_victim_hosts(router_pool, scenario=scenario)
            params["host_name"] = _choice(
                rng, victim_pool, _first(victim_pool) or router0
            )

    elif problem in {
        "bgp_acl_block",
        "bgp_asn_misconfig",
        "ospf_acl_block",
        "ospf_area_misconfiguration",
        "ospf_neighbor_missing",
        "frr_service_down",
    }:
        params["host_name"] = router0

    elif problem == "bgp_rpki_invalid_route_leak":
        from nika.net_env.isp.inject_targets import isp_inject_params

        inventory = getattr(net_env, "inventory", None)
        bgp_inv = inventory.get("bgp") if isinstance(inventory, dict) else None
        if not isinstance(bgp_inv, dict) or not bgp_inv.get("rpki"):
            from nika.net_env.isp.bgp import compile_bgp_plan
            from nika.net_env.isp.igp import IspConfig, compile_isp_plan
            from nika.workflows.benchmark.isp_options import isp_config_for_problem

            isp_opts = isp_options or isp_config_for_problem(problem, {"rpki"})
            isp_plan = compile_isp_plan(
                IspConfig(
                    topology=isp_opts["topo"],
                    igp=isp_opts["igp"],  # type: ignore[arg-type]
                )
            )
            bgp = compile_bgp_plan(
                isp_plan, isp_opts["bgp_mode"], rpki=bool(isp_opts.get("rpki"))
            )
            assert bgp is not None
            bgp_inv = bgp.inventory
            inventory = getattr(net_env, "inventory", None) or {}
            if not isinstance(inventory, dict):
                inventory = {}
        params.update(
            isp_inject_params(
                problem,
                inventory if isinstance(inventory, dict) else {},
                bgp_inv,
            )
        )

    elif problem == "bgp_max_prefix_exceeded":
        from nika.net_env.isp.inject_targets import first_ebgp_session

        bgp_inv = None
        inventory = getattr(net_env, "inventory", None)
        if isinstance(inventory, dict):
            bgp_inv = inventory.get("bgp")
        if not isinstance(bgp_inv, dict):
            from nika.net_env.isp.bgp import compile_bgp_plan
            from nika.net_env.isp.igp import IspConfig, compile_isp_plan
            from nika.workflows.benchmark.isp_options import isp_config_for_problem

            isp_opts = isp_options or isp_config_for_problem(problem, {"bgp"})
            isp_plan = compile_isp_plan(
                IspConfig(
                    topology=isp_opts["topo"],
                    igp=isp_opts["igp"],  # type: ignore[arg-type]
                )
            )
            bgp = compile_bgp_plan(isp_plan, isp_opts["bgp_mode"])
            assert bgp is not None
            bgp_inv = bgp.inventory
        params.update(first_ebgp_session(bgp_inv))
        params["flood_count"] = "120"

    elif problem in {"arp_acl_block", "icmp_acl_block", "http_acl_block"}:
        if scenario == "enterprise_branch":
            params["host_name"] = "br1_corp_pc" if "br1_corp_pc" in host_pool else host0
        elif scenario == "k8s_lab":
            params["host_name"] = "client" if "client" in host_pool else host0
        elif scenario in {"p4_dc_fabric", "sdn_l3_clos", "min3clos"}:
            params["host_name"] = "client_1_1" if "client_1_1" in host_pool else host0
        else:
            params["host_name"] = host0

    elif problem == "dns_port_blocked":
        params["host_name"] = dns0

    elif problem == "mac_address_conflict":
        a, b = _mac_conflict_pair(net_env, rng)
        params["host_name"] = a
        params["host_name_2"] = b

    elif problem in {
        "p4_table_entry_missing",
        "p4_table_entry_misconfig",
        "bmv2_switch_down",
        "p4_action_selector_member_misconfig",
        "p4_ecmp_group_member_missing",
        "p4runtime_pipeline_mismatch",
        "p4runtime_partial_write",
        "p4_table_resource_exhaustion",
    }:
        if problem in {
            "p4_action_selector_member_misconfig",
            "p4_ecmp_group_member_missing",
            "p4runtime_pipeline_mismatch",
            "p4runtime_partial_write",
            "p4_table_resource_exhaustion",
        }:
            leaves = [n for n in bmv2 if str(n).startswith("leaf_")]
            params["host_name"] = _choice(rng, leaves, "leaf_1")
        elif problem == "bmv2_switch_down" and scenario == "p4_dc_fabric":
            leaves = [n for n in bmv2 if str(n).startswith("leaf_")]
            params["host_name"] = (
                "leaf_1" if "leaf_1" in leaves else _first(leaves) or "leaf_1"
            )
        elif (
            problem in {"p4_table_entry_missing", "p4_table_entry_misconfig"}
            and scenario == "p4_dc_fabric"
        ):
            leaves = [n for n in bmv2 if str(n).startswith("leaf_")]
            params["host_name"] = (
                "leaf_1" if "leaf_1" in leaves else _first(leaves) or "leaf_1"
            )
        elif problem == "p4runtime_pipeline_mismatch" and scenario == "p4_dc_fabric":
            leaves = [n for n in bmv2 if str(n).startswith("leaf_")]
            params["host_name"] = (
                "leaf_1" if "leaf_1" in leaves else _first(leaves) or "leaf_1"
            )
        else:
            params["host_name"] = _choice(rng, bmv2, host0)

    elif problem in {
        "sdn_controller_crash",
        "southbound_port_block",
        "southbound_port_mismatch",
    }:
        controller_pool = pools.get("controllers") or controllers
        params["host_name"] = _choice(rng, controller_pool, host0)
        if problem == "southbound_port_block":
            params["southbound_port"] = "6653"
        if problem == "southbound_port_mismatch":
            params["mismatched_port"] = "6633"
            params["original_port"] = "6653"

    elif problem == "flow_rule_shadowing":
        params["host_name"] = _choice(rng, net_env.ovs_switches, host0)

    elif problem == "flow_rule_loop":
        a, b, port_a, port_b = _flow_rule_loop_pair(net_env, rng)
        params["host_name"] = a
        params["host_name_2"] = b
        params["port_name"] = port_a
        params["port_name_2"] = port_b

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
        elif scenario == "dc_clos":
            # Keep victim, observer, and URL on one deterministic HTTP path.
            params.update(
                host_name="webserver0_pod0",
                attacker_device="client_0",
                observer_device="dns_pod0",
                probe_url="http://10.0.1.2/small.bin",
            )
        elif scenario == "campus_lan":
            params.update(
                host_name="web_server_0",
                attacker_device="pc_1_1_1_1",
                observer_device="pc_2_1_1_1",
                probe_url="http://10.200.0.3/",
            )
        elif scenario == "enterprise_branch":
            params.update(
                host_name="hq_srv",
                attacker_device="br1_corp_pc",
                observer_device="hq_corp_pc",
                probe_url="http://10.0.20.2/small.bin",
                attack_url="http://10.0.20.2/nika-dos-dir/",
            )
        elif scenario in {"sdn_l3_clos", "p4_dc_fabric"}:
            model = net_env.model
            observer = model.client_endpoints()[0]
            victim = next(
                web for web in model.web_endpoints() if web.leaf_id != observer.leaf_id
            )
            attacker = next(
                client
                for client in reversed(model.client_endpoints())
                if client.name != observer.name
            )
            params.update(
                host_name=victim.name,
                attacker_device=attacker.name,
                observer_device=observer.name,
                probe_url=f"http://{victim.ip}/",
            )
        elif scenario == "p4_dc_gateway":
            model = net_env.model
            victim = model.backend_pool[0]
            observer = model.clients[0]
            attacker = model.clients[-1]
            params.update(
                host_name=victim.name,
                attacker_device=attacker.name,
                observer_device=observer.name,
                probe_url=model.vip_url,
                attack_url=model.vip_url,
            )
        else:
            params["host_name"] = web0
            params["attacker_device"] = _pick_attacker(
                rng,
                hosts,
                web0,
                host0,
                pool=pools.get("attacker_pool"),
            )

    elif problem == "dns_lookup_latency":
        dns_target = dns0 if dns0 in _all_device_names(net_env) else host0
        params["host_name"] = dns_target
        params["intf_name"] = _choice_interface(rng, net_env, dns_target, backend)
        params["delay_ms"] = "1000"

    elif problem == "incast_traffic_network_limitation":
        web_pool = pools.get("web") or servers.get("web") or []
        if scenario == "enterprise_branch":
            server_pool = [h for h in (pools.get("hosts") or []) if h.endswith("_srv")]
            target = (
                "hq_srv"
                if "hq_srv" in server_pool
                else (web0 if web0 in web_pool else host0)
            )
            params["host_name"] = target
            params["rate"] = "256kbit"
            params["burst"] = "128kb"
            params["limit"] = "128kb"
            params["delay_ms"] = "80"
        else:
            params["host_name"] = web0 if web0 in web_pool else host0
            params["rate"] = "512kbit" if scenario == "enterprise_branch" else "1mbit"
            params["burst"] = "256kb" if scenario == "enterprise_branch" else "500kb"
            params["limit"] = "256kb" if scenario == "enterprise_branch" else "500kb"
            params["delay_ms"] = (
                "50" if scenario in {"campus_lan", "sdn_l3_clos"} else "20"
            )
        if scenario == "p4_dc_gateway":
            params["delay_ms"] = "80"
            params["rate"] = "512kbit"

    elif problem == "tcp_receive_window_limited":
        # Fault the HTTP client (TCP receiver of server→client bulk download).
        if scenario == "enterprise_branch":
            params["host_name"] = (
                "br1_corp_pc"
                if "br1_corp_pc" in host_pool
                else (_first(host_pool) or host0)
            )
            params["sender_host"] = "hq_srv"
            params["sender_ip"] = "10.0.20.2"
            params["small_url"] = "http://10.0.20.2/small.bin"
            params["large_url"] = "http://10.0.20.2/large.bin"
        else:
            params["host_name"] = host0

    elif problem == "sender_resource_contention":
        # Prefer real HTTP servers over client pools that some scenarios label "web".
        web_pool = list(servers.get("web") or []) or list(pools.get("web") or [])
        if scenario == "enterprise_branch":
            params["host_name"] = (
                "hq_srv"
                if "hq_srv" in web_pool
                else (web0 if web0 in web_pool else (_first(web_pool) or host0))
            )
        elif scenario == "dc_clos":
            params["host_name"] = (
                "webserver0_pod0"
                if "webserver0_pod0" in web_pool
                else (_first(web_pool) or web0 or host0)
            )
            params["client_host"] = "client_0"
            params["dst_ip"] = "10.0.1.2"
            params["small_url"] = "http://web0.pod0/small.bin"
            params["large_url"] = "http://web0.pod0/large.bin"
            params["cpu_quota"] = "0.05"
            params["stress_cpus"] = "16"
            params["baseline_trials"] = "5"
        else:
            params["host_name"] = (
                web0 if web0 in web_pool else (_first(web_pool) or host0)
            )
        params["duration"] = "900" if scenario == "enterprise_branch" else "600"

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

    elif problem == "k8s_networkpolicy_deny":
        k8s_nodes = pools.get("k8s_nodes") or []
        control = _first(pools.get("k8s_controllers")) or _first(k8s_nodes) or host0
        params["control_node"] = control
        params["symptom_host"] = _first(pools.get("hosts")) or host0
        if scenario == "llmd_lab":
            params["namespace"] = "llm-d"
            params["pod_selector"] = "app=llm-d-pd"
            params["symptom_url"] = "http://llmd/v1/models"
            params["control_url"] = ""
        else:
            params["namespace"] = "word-ns"
            params["pod_selector"] = "app=word"
            params["symptom_url"] = "http://datacenter.com/word"
            params["control_url"] = "http://datacenter.com/weather"

    elif problem == "load_balancer_overload":
        # Fixed capacity + HTTP surge on campus_lan NGINX VIP (web99.local).
        # Workload knobs are fixed for reproducibility (not searched at runtime).
        pc_pool = [h for h in host_pool if h.startswith("pc_")] or list(host_pool)
        probe_client = (
            "pc_1_1_1_1" if "pc_1_1_1_1" in pc_pool else _choice(rng, pc_pool, host0)
        )
        load_candidates = [h for h in pc_pool if h != probe_client]
        if not load_candidates:
            load_candidates = [probe_client]
        # Prefer a second building PC when present so probe and load are separate.
        load_hosts = []
        for preferred in ("pc_2_1_1_1", "pc_3_1_1_1", "pc_1_2_1_1"):
            if preferred in load_candidates and preferred not in load_hosts:
                load_hosts.append(preferred)
            if len(load_hosts) >= 2:
                break
        while len(load_hosts) < 2 and load_candidates:
            pick = load_candidates[len(load_hosts) % len(load_candidates)]
            if pick not in load_hosts:
                load_hosts.append(pick)
            else:
                break
        if not load_hosts:
            load_hosts = [probe_client]
        params.update(
            host_name=lb0 if lb0 else "load_balancer",
            client_host=probe_client,
            load_client_hosts=",".join(load_hosts),
            vip_url="http://web99.local/small",
            control_url="http://web0.local/small",
            backend_url="http://20.200.0.2/small",
            backend_probe_host="load_balancer",
            backend_cpu_host="backend_web_0",
            cpu_quota="0.2",
            concurrency="160",
            load_workers="2",
            warmup_sec="5",
            probe_requests="60",
            probe_concurrency="4",
            duration_sec="300",
        )

    else:
        params["host_name"] = host0

    if scenario == "isp":
        from nika.net_env.isp.inject_targets import enrich_isp_symptom_params

        inventory = getattr(net_env, "inventory", None)
        if not isinstance(inventory, dict):
            inventory = {}
        bgp_inv = inventory.get("bgp")
        enrich_isp_symptom_params(
            params,
            problem,
            inventory,
            bgp_inv if isinstance(bgp_inv, dict) else None,
        )

    return params


def validate_benchmark_case(
    scenario: str,
    problem: str,
    inject: dict[str, str],
    topo_size: str = "",
    *,
    isp_options: dict[str, str] | None = None,
) -> None:
    """Raise ValueError if a benchmark row is inconsistent with tags or topology."""
    from nika.net_env.net_env_pool import resolve_scenario_id, scenario_tags

    problems = list_avail_problem_instances()
    canonical = resolve_scenario_id(scenario)
    try:
        registered_tags = scenario_tags(canonical)
    except ValueError:
        raise ValueError(f"Unknown scenario {scenario!r}") from None
    if problem not in problems:
        raise ValueError(f"Unknown problem {problem!r}")

    problem_tags = set(problems[problem].TAGS)
    available_tags = set(registered_tags)
    if not problem_tags.issubset(available_tags):
        raise ValueError(
            f"Tag mismatch for {problem} on {scenario}: "
            f"problem tags {sorted(problem_tags)} not subset of scenario tags {sorted(available_tags)}"
        )

    net_env = _get_net_env_for_benchmark(
        canonical,
        topo_size,
        isp_options=isp_options,
    )
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
    # WireGuard tunnel ifaces are not Kathara L2 link endpoints; validate below.
    if host_name and intf_name and problem not in _WG_TUNNEL_IFACE_PROBLEMS:
        device_ifaces = ifaces_by_device.get(host_name) or []
        if device_ifaces and intf_name not in device_ifaces:
            raise ValueError(
                f"Inject interface {intf_name!r} not on {host_name!r} in {scenario} "
                f"(topo_size={topo_size!r}); known interfaces: {device_ifaces}"
            )

    if problem == "device_forwarding_packet_corruption":
        target = inject.get("forwarding_device", "")
        target_intf = inject.get("intf_name", "")
        forwarding_devices = set(net_env.routers or []) | set(net_env.switches or [])
        if target not in forwarding_devices:
            raise ValueError(
                "device_forwarding_packet_corruption requires a router or switch target"
            )
        if target_intf not in (ifaces_by_device.get(target) or []):
            raise ValueError(
                f"forwarding interface {target_intf!r} is not on {target!r}"
            )

    if problem == "wireguard_peer_key_misconfiguration":
        from nika.net_env.net_env_pool import is_enterprise_branch_scenario

        if not is_enterprise_branch_scenario(scenario):
            raise ValueError(
                f"wireguard_peer_key_misconfiguration requires enterprise_branch (got {scenario!r})"
            )
        eligible = _primary_hq_wg_targets(topo_size)
        pair = (host_name or "", intf_name or "")
        if pair not in eligible:
            raise ValueError(
                f"wireguard_peer_key_misconfiguration target {pair!r} is not a "
                f"primary HQ tunnel on {scenario} (topo_size={topo_size!r}); "
                f"eligible: {eligible}"
            )

    if problem == "wireguard_allowed_ips_misconfiguration":
        from nika.net_env.net_env_pool import is_enterprise_branch_scenario

        if not is_enterprise_branch_scenario(scenario):
            raise ValueError(
                f"wireguard_allowed_ips_misconfiguration requires enterprise_branch (got {scenario!r})"
            )
        eligible = _primary_hq_wg_targets(topo_size)
        pair = (host_name or "", intf_name or "")
        if pair not in eligible:
            raise ValueError(
                f"wireguard_allowed_ips_misconfiguration target {pair!r} is not a "
                f"primary HQ tunnel on {scenario} (topo_size={topo_size!r}); "
                f"eligible: {eligible}"
            )
        target_prefix = inject.get("target_prefix")
        if target_prefix:
            spoke = (
                host_name[: -len("_edge")]
                if host_name and host_name.endswith("_edge")
                else (host_name or "")
            )
            remotes = _remote_prefixes_for_spoke(topo_size, spoke)
            if target_prefix not in remotes:
                raise ValueError(
                    f"wireguard_allowed_ips_misconfiguration target_prefix="
                    f"{target_prefix!r} is not a remote advertised prefix for "
                    f"{spoke!r} (topo_size={topo_size!r}); remotes: {remotes}"
                )

    if problem == "vrf_dscp_remarking":
        from nika.net_env.net_env_pool import is_enterprise_branch_scenario

        if not is_enterprise_branch_scenario(scenario):
            raise ValueError(
                f"vrf_dscp_remarking requires enterprise_branch (got {scenario!r})"
            )
        eligible = _dscp_remark_targets(topo_size)
        eligible_keys = {
            (t.edge, t.intf_name, t.src_host, t.dst_host) for t in eligible
        }
        key = (
            host_name or "",
            intf_name or "",
            inject.get("src_host") or "",
            inject.get("dst_host") or "",
        )
        if key not in eligible_keys:
            raise ValueError(
                f"vrf_dscp_remarking target {key!r} is not an eligible "
                f"LAN→overlay CORP path on {scenario} (topo_size={topo_size!r}); "
                f"eligible count={len(eligible_keys)}"
            )

    if problem == "bgp_missing_route_advertisement" and host_name:
        routers = net_env.routers or []
        advertisers = _routers_with_bgp_network(routers, scenario=scenario)
        # Enforce only when the topology distinguishes advertiser roles.
        if advertisers != list(routers) and host_name not in advertisers:
            raise ValueError(
                f"bgp_missing_route_advertisement host_name={host_name!r} has no BGP "
                f"network statement on {scenario} (topo_size={topo_size!r}); "
                f"use a leaf router: {advertisers}"
            )

    if problem in _VICTIM_HOST_PROBLEMS and host_name:
        routers = net_env.routers or []
        eligible = _routers_with_victim_hosts(routers, scenario=scenario)
        # Enforce only when the topology distinguishes leaf vs spine roles.
        if eligible != list(routers) and host_name not in eligible:
            raise ValueError(
                f"{problem} host_name={host_name!r} has no attached end host on "
                f"{scenario} (topo_size={topo_size!r}); use a leaf router: {eligible}"
            )

    host_a = inject.get("host_name")
    host_b = inject.get("host_name_2")
    if host_a and host_b and host_a == host_b:
        hosts = net_env.hosts or []
        server_hosts = []
        for bucket in (net_env.servers or {}).values():
            server_hosts.extend(bucket or [])
        conflict_pool = list(dict.fromkeys(list(hosts) + server_hosts))
        if problem == "host_ip_conflict" and len(conflict_pool) >= 2:
            raise ValueError(
                f"Inject devices host_name and host_name_2 must differ for {problem} "
                f"on {scenario} when multiple hosts exist"
            )
        if problem == "mac_address_conflict" and len(conflict_pool) >= 2:
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
