"""Resolve inject parameters when generating benchmark YAML (offline only)."""

from __future__ import annotations

import hashlib
import random
from collections import defaultdict

from nika.net_env.net_env_pool import get_net_env_instance
from nika.problems.registry import list_avail_problem_instances
from nika.problems.support.benchmark_targets import (
    InjectTargetContext,
    InjectValidationContext,
    access_router_victim as _access_router_victim,
    all_device_names as _all_device_names,
    arp_l2_endpoint_host as _arp_l2_endpoint_host,
    bmv2_leaves as _bmv2_leaves,
    choice as _choice,
    choice_distinct as _choice_distinct,
    choice_interface as _choice_interface,
    device_interfaces as _device_interfaces,
    dhcp_server_client as _dhcp_server_client,
    endpoint_target as _endpoint_target,
    first as _first,
    k8s_control_node as _k8s_control_node,
    p4_gateway_port_target as _p4_gateway_port_target,
    prefer_named as _prefer_named,
    prefer_prefixed_node as _prefer_prefixed_node,
    require_distinct_hosts as _require_distinct_hosts,
    require_role_member as _require_role_member,
    resolve_link_flap_params as _resolve_link_flap_params,
    resolve_path_mtu_target as _resolve_path_mtu_target,
    role_subset as _role_subset,
)
from nika.workflows.benchmark.isp_options import (
    is_isp_base_topology,
    is_isp_scenario,
    isp_topo_from_scenario,
)

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
        f"{isp_options.get('igp', '')}|"
        f"{isp_options.get('bgp_mode', '')}|"
        f"{isp_options.get('rpki', False)}|"
        f"{isp_options.get('backend', '')}|"
        f"{isp_options.get('device_profile', '')}"
    )


def _isp_protocol_kwargs(
    scenario: str, isp_options: dict[str, str] | None
) -> dict[str, object]:
    """Protocol kwargs for get_net_env_instance (topo comes from deploy_defaults)."""
    if not is_isp_base_topology(scenario) or not isp_options:
        return {}
    kwargs: dict[str, object] = {}
    for key in ("igp", "bgp_mode", "rpki"):
        if key in isp_options and isp_options[key] not in (None, "", "-"):
            kwargs[key] = isp_options[key]
    return kwargs


def _resolve_benchmark_backend(
    scenario: str, isp_options: dict[str, str] | None
) -> str:
    from nika.net_env.isp.profiles import DEFAULT_BACKEND_FOR_ISP
    from nika.net_env.net_env_pool import resolve_scenario_backend

    requested = None
    if isp_options:
        raw = isp_options.get("backend")
        if raw not in (None, "", "-"):
            requested = str(raw)
    return resolve_scenario_backend(
        scenario,
        backend=requested,
        default_when_ambiguous=DEFAULT_BACKEND_FOR_ISP,
    )


def _isp_stack_kwargs(
    scenario: str, isp_options: dict[str, str] | None
) -> dict[str, object]:
    """Backend / device_profile kwargs for ISP (and other multi-backend) labs."""
    kwargs: dict[str, object] = {
        "backend": _resolve_benchmark_backend(scenario, isp_options),
    }
    if isp_options:
        profile = isp_options.get("device_profile")
        if profile not in (None, "", "-"):
            kwargs["device_profile"] = str(profile)
    return kwargs


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


def _br1_primary_wan_iface(topo_size: str) -> str:
    """Primary ISP WAN on br1_edge (hosts GUEST NAT /32 aliases)."""
    from nika.net_env.enterprise_branch.topology import SCALE

    size = topo_size if topo_size in SCALE else "s"
    # LANs occupy eth0..ethN-1; first WAN is ethN.
    return f"eth{len(SCALE[size].branch_roles)}"


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
        "bgp_hijacking",
    }
)


def _peer_of_host(net_env, host_name: str) -> tuple[str, str] | None:
    """Return the device/intf on the other end of a host attachment link."""
    from nika.problems.rca.inventory import iter_link_termination_points, parse_endpoint

    needle = f"{host_name}:"
    for _key, tps in iter_link_termination_points(net_env):
        endpoints = [str(ep) for ep in tps]
        host_ep = next((ep for ep in endpoints if ep.startswith(needle)), None)
        if host_ep is None or len(endpoints) != 2:
            continue
        other = endpoints[0] if endpoints[1] == host_ep else endpoints[1]
        return parse_endpoint(other)
    return None


def _spine_egress_toward_leaf(net_env, leaf_router: str) -> tuple[str, str] | None:
    """Return spine egress intf toward a leaf router."""
    import re

    from nika.problems.rca.inventory import iter_link_termination_points, parse_endpoint

    matches: list[tuple[str, str]] = []
    for _key, tps in iter_link_termination_points(net_env):
        endpoints = [str(ep) for ep in tps]
        if len(endpoints) != 2:
            continue
        for idx, ep in enumerate(endpoints):
            host, _intf = parse_endpoint(ep)
            if host != leaf_router:
                continue
            other_ep = endpoints[1 - idx]
            other_host, other_intf = parse_endpoint(other_ep)
            if other_host.startswith("spine_router_"):
                matches.append((other_host, other_intf))
    if not matches:
        return None
    leaf_match = re.search(r"leaf_router_(\d+)_(\d+)", leaf_router)
    if leaf_match is not None:
        preferred = f"spine_router_{leaf_match.group(1)}_{leaf_match.group(2)}"
        for host, intf in matches:
            if host == preferred:
                return host, intf
    return sorted(matches)[0]


def _leaf_router_for_web(net_env, web_host: str) -> tuple[str, str] | None:
    """Return the leaf router and its host-facing intf for a web service."""
    from nika.problems.rca.inventory import iter_link_termination_points, parse_endpoint

    needle = f"{web_host}:"
    for _key, tps in iter_link_termination_points(net_env):
        endpoints = [str(ep) for ep in tps]
        web_ep = next((ep for ep in endpoints if ep.startswith(needle)), None)
        if web_ep is None or len(endpoints) != 2:
            continue
        other = endpoints[0] if endpoints[1] == web_ep else endpoints[1]
        peer_host, peer_intf = parse_endpoint(other)
        if peer_host.startswith("leaf_router_"):
            return peer_host, peer_intf
    return None


def _resolve_device_forwarding_corruption_params(
    scenario: str,
    net_env,
    rng: random.Random,
    *,
    routers: list[str],
    switches: list[str],
    servers: dict[str, list[str]],
    web0: str,
    seed: int,
    topo_size: str,
) -> dict[str, str]:
    """Pin forwarding-device corruption on the default probe path."""
    forwarding = list(routers) + list(switches or [])
    if not forwarding:
        raise ValueError(
            f"device_forwarding_packet_corruption requires a forwarding device in {scenario}"
        )
    flap = _resolve_link_flap_params(
        scenario,
        net_env,
        rng,
        "kathara",
        host_pool=[],
        host0="",
        router0="",
    )
    params: dict[str, str] = {"seed": str(seed)}
    if flap.get("probe_dst_ip"):
        params["probe_dst_ip"] = flap["probe_dst_ip"]
    if flap.get("observer_device"):
        params["observer_device"] = flap["observer_device"]

    if scenario == "sdn_l3_clos":
        model = getattr(net_env, "model", None)
        if model is not None and getattr(model, "client_endpoints", None):
            observer = model.client_endpoints()[0]
            victim = next(
                web for web in model.web_endpoints() if web.leaf_id != observer.leaf_id
            )
            web_leaf = f"leaf_{victim.leaf_id}"
            port = model.port_to_peer(web_leaf, victim.name)
            params.update(
                forwarding_device=web_leaf,
                intf_name=port.name if port is not None else "eth0",
                probe_dst_ip=victim.ip,
                observer_device=observer.name,
            )
            return params

    if scenario == "dc_clos":
        web_hosts = list(servers.get("web") or [])
        web = _prefer_named(web_hosts, "webserver0_pod0", _first(web_hosts) or web0)
        leaf_match = _leaf_router_for_web(net_env, web) if web else None
        if leaf_match is not None:
            leaf_router, _leaf_intf = leaf_match
            spine_match = _spine_egress_toward_leaf(net_env, leaf_router)
            if spine_match is not None:
                spine, spine_intf = spine_match
                params.update(
                    forwarding_device=spine,
                    intf_name=spine_intf,
                )
                return params

    if scenario == "campus_lan":
        core_candidates = [n for n in routers if "router_core" in n] or list(routers)
        target = (
            "router_core_2"
            if "router_core_2" in core_candidates
            else _choice(rng, core_candidates, core_candidates[0])
        )
        ifaces = _device_interfaces(net_env).get(target) or ["eth0"]
        params.update(
            forwarding_device=target,
            intf_name=ifaces[-1] if ifaces else "eth0",
        )
        return params

    if scenario == "enterprise_branch":
        edge = (
            "hq_edge"
            if "hq_edge" in routers
            else _choice(rng, list(routers), routers[0])
        )
        ifaces = _device_interfaces(net_env).get(edge) or []
        corp_ifaces = sorted(i for i in ifaces if not i.startswith("wg_"))
        params.update(
            forwarding_device=edge,
            intf_name=corp_ifaces[-1] if corp_ifaces else "eth0",
        )
        return params

    target = _choice(rng, forwarding, forwarding[0])
    interfaces = _device_interfaces(net_env).get(target) or []
    params.update(
        forwarding_device=target,
        intf_name=interfaces[-1] if interfaces else "eth0",
    )
    return params


def _parse_web_url(url: str) -> tuple[str, str]:
    website = url.split(".")[0]
    if website.startswith("http://"):
        website = website[len("http://") :]
    domain = url.split(".")[1] if "." in url else "local"
    return website, domain


def _dns_host_for_domain(
    net_env, domain: str, fallback: str | None = None
) -> str | None:
    """Return the DNS server that serves ``domain`` (e.g. dns_pod1 -> pod1)."""
    dns_servers = list((getattr(net_env, "servers", None) or {}).get("dns") or [])
    if not dns_servers:
        return fallback
    if len(dns_servers) == 1:
        return dns_servers[0]
    for host in dns_servers:
        # dc_clos: dns_pod{N} owns zone pod{N}
        if host.startswith("dns_") and host[len("dns_") :] == domain:
            return host
    return fallback or dns_servers[0]


def _dns_record_targets(net_env, rng: random.Random) -> tuple[str, str]:
    urls = getattr(net_env, "web_urls", None) or []
    if urls:
        return _parse_web_url(rng.choice(urls))
    web_pool = net_env.servers.get("web") or []
    web = _choice(rng, web_pool, "web0")
    if web:
        return web.replace("web_server_", "web"), "local"
    return "web0", "local"


def _align_dns_record_inject(net_env, row: dict[str, str]) -> dict[str, str]:
    """When host is dns_<zone> and zone exists in web_urls, keep domain/website aligned."""
    out = dict(row)
    host = out.get("host_name") or ""
    if not host.startswith("dns_"):
        return out
    candidate = host[len("dns_") :]
    urls = getattr(net_env, "web_urls", None) or []
    # dns_server must not become domain "server"; only remap for real zones (e.g. pod0).
    match = next((url for url in sorted(urls) if candidate in url), None)
    if match is None:
        return out
    website, domain = _parse_web_url(match)
    out["target_domain"] = domain
    out["target_website"] = website
    return out


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


def _get_net_env_for_benchmark(
    scenario: str,
    topo_size: str = "",
    *,
    isp_options: dict[str, str] | None = None,
):
    kwargs: dict = {}
    if topo_size and not is_isp_scenario(scenario):
        kwargs["topo_size"] = topo_size
    kwargs.update(_isp_protocol_kwargs(scenario, isp_options))
    kwargs.update(_isp_stack_kwargs(scenario, isp_options))
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


def _owns_benchmark_targets(problem_cls) -> bool:
    """Whether a failure declares its own benchmark targets (else legacy)."""
    return problem_cls is not None and "benchmark_inject_params" in vars(problem_cls)


def _resolve(
    problem: str,
    scenario: str,
    topo_size: str,
    *,
    seed: int,
    isp_options: dict[str, str] | None,
    net_env,
) -> tuple[dict[str, str], InjectTargetContext]:
    rng = _case_rng(
        seed,
        scenario,
        problem,
        topo_size,
        _isp_rng_key(isp_options),
    )
    if net_env is None:
        net_env = _get_net_env_for_benchmark(
            scenario, topo_size, isp_options=isp_options
        )
    _load_inventory(net_env)
    backend = _resolve_benchmark_backend(scenario, isp_options)

    hosts = net_env.hosts or []
    routers = net_env.routers or []
    servers = net_env.servers or {}
    bmv2 = net_env.bmv2_switches or []
    controllers = net_env.sdn_controllers or []

    pools = net_env.target_roles()
    host_pool = pools.get("hosts") or hosts
    router_pool = pools.get("routers") or routers

    host0 = _choice(rng, host_pool, _first(hosts) or "pc1")
    router0 = _choice(rng, router_pool, _first(routers) or host0)
    dns0 = _choice(rng, servers.get("dns"), host0)
    dhcp0 = _choice(rng, servers.get("dhcp"), dns0)
    web0 = _choice(rng, pools.get("web") or servers.get("web"), host0)
    lb0 = _choice(rng, servers.get("load_balancer"), web0)

    ctx = InjectTargetContext(
        problem=problem,
        scenario=scenario,
        topo_size=topo_size,
        seed=seed,
        isp_options=isp_options,
        net_env=net_env,
        rng=rng,
        backend=backend,
        roles=pools,
        hosts=hosts,
        routers=routers,
        switches=net_env.switches or [],
        servers=servers,
        bmv2=bmv2,
        controllers=controllers,
        host_pool=host_pool,
        router_pool=router_pool,
        host0=host0,
        router0=router0,
        dns0=dns0,
        dhcp0=dhcp0,
        web0=web0,
        lb0=lb0,
    )
    problem_cls = list_avail_problem_instances().get(problem)
    if _owns_benchmark_targets(problem_cls):
        params = problem_cls.benchmark_inject_params(ctx)
    else:
        params = _legacy_inject_params(problem, ctx)

    if is_isp_scenario(scenario):
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

    return params, ctx


def resolve_inject_params(
    problem: str,
    scenario: str,
    topo_size: str = "",
    *,
    seed: int = DEFAULT_SEED,
    isp_options: dict[str, str] | None = None,
    net_env=None,
) -> dict[str, str]:
    """Return inject params for one benchmark row."""
    params, _ctx = _resolve(
        problem,
        scenario,
        topo_size,
        seed=seed,
        isp_options=isp_options,
        net_env=net_env,
    )
    return params


def _legacy_inject_params(problem: str, ctx: InjectTargetContext) -> dict[str, str]:
    """Name-dispatched targets for failures without ``benchmark_inject_params``."""
    rng = ctx.rng
    scenario = ctx.scenario
    topo_size = ctx.topo_size
    seed = ctx.seed
    isp_options = ctx.isp_options
    net_env = ctx.net_env
    backend = ctx.backend
    hosts = ctx.hosts
    routers = ctx.routers
    servers = ctx.servers
    bmv2 = ctx.bmv2
    controllers = ctx.controllers
    pools = ctx.roles
    host_pool = ctx.host_pool
    router_pool = ctx.router_pool
    host0 = ctx.host0
    router0 = ctx.router0
    dns0 = ctx.dns0
    web0 = ctx.web0
    lb0 = ctx.lb0

    params: dict[str, str] = {}

    if problem == "device_forwarding_packet_corruption":
        params.update(
            _resolve_device_forwarding_corruption_params(
                scenario,
                net_env,
                rng,
                routers=list(routers),
                switches=list(net_env.switches or []),
                servers=servers,
                web0=web0,
                seed=seed,
                topo_size=topo_size,
            )
        )

    elif scenario == "p4_dc_gateway" and problem in {
        "lb_connection_state_exhaustion",
        "lb_pending_connection_update_race",
        "icmp_frag_needed_filter_misconfiguration",
    }:
        params["host_name"] = "gateway_1"
        if problem == "lb_connection_state_exhaustion":
            model = net_env.model
            params.update(
                capacity="256",
                syn_timeout_sec="10",
                seed=str(seed),
                client_host=model.clients[0].name,
                vip_url=model.vip_url,
                backend_dip=model.backend_pool[0].ip,
                attacker_device=model.clients[-1].name,
            )
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
            params.update(
                _p4_gateway_port_target(
                    ctx, gateways_only=problem == "int_insufficient_mtu_headroom"
                )
            )
            if problem == "p4_ecn_threshold_misconfiguration":
                params["threshold"] = "1024"
            else:
                params["int_mtu"] = "1480"

    elif problem in {
        "host_missing_ip",
        "host_incorrect_ip",
        "host_incorrect_gateway",
        "host_incorrect_netmask",
        "host_incorrect_dns",
        "arp_cache_poisoning",
        "receiver_resource_contention",
    }:
        params.update(
            _endpoint_target(
                ctx,
                l2_endpoint=problem == "arp_cache_poisoning",
                with_intf=problem == "host_missing_ip",
            )
        )
        if problem == "arp_cache_poisoning" and scenario == "llmd_lab":
            # Star L2 lab has no default route; poison the HTTP peer.
            params["target_ip"] = "200.0.0.8"
        if problem == "host_incorrect_netmask":
            params["netmask_prefix"] = "8"
        if problem == "receiver_resource_contention":
            params["duration"] = "600"
            if scenario == "dc_clos":
                params["peer_host"] = "webserver0_pod0"
                params["large_url"] = "http://web0.pod0/large.bin"
                params["stress_cpus"] = "4"

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
                wan_interface=_br1_primary_wan_iface(topo_size),
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
        params["target_website"] = website
        params["target_domain"] = domain
        params["host_name"] = (
            _dns_host_for_domain(net_env, domain, fallback=dns0) or dns0
        )

    elif problem in {"dns_service_down"}:
        params["host_name"] = dns0

    elif problem in {"dhcp_service_down", "dhcp_missing_subnet"}:
        params.update(_dhcp_server_client(ctx))
        if scenario == "campus_lan" and problem == "dhcp_missing_subnet":
            params["subnet"] = "10.1.1.0"

    elif problem in {"dhcp_spoofed_gateway", "dhcp_spoofed_dns", "dhcp_spoofed_subnet"}:
        params.update(_dhcp_server_client(ctx))
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
        if is_isp_scenario(scenario):
            from nika.net_env.isp.inject_targets import isp_inject_params

            inventory = getattr(net_env, "inventory", None)
            if not isinstance(inventory, dict):
                inventory = {}
            bgp_inv = inventory.get("bgp")
            if not isinstance(bgp_inv, dict) or not bgp_inv.get("originated"):
                from nika.net_env.isp.bgp import compile_bgp_plan
                from nika.net_env.isp.igp import IspConfig, compile_isp_plan
                from nika.workflows.benchmark.isp_options import isp_config_for_problem

                isp_opts = isp_options or isp_config_for_problem(problem, {"bgp"})
                rtbh = scenario.endswith("_ebgp_rtbh") or bool(isp_opts.get("rtbh"))
                isp_plan = compile_isp_plan(
                    IspConfig(
                        topology=isp_topo_from_scenario(scenario),
                        igp=str(isp_opts.get("igp") or "ospf"),  # type: ignore[arg-type]
                    )
                )
                bgp = compile_bgp_plan(
                    isp_plan,
                    str(isp_opts.get("bgp_mode") or "ebgp"),
                    rpki=bool(isp_opts.get("rpki")),
                    rtbh=rtbh,
                )
                assert bgp is not None
                bgp_inv = bgp.inventory
                inventory = dict(isp_plan.inventory)
                inventory["bgp"] = bgp_inv
            params.update(isp_inject_params(problem, inventory, bgp_inv))
        else:
            advertise_pool = _role_subset(router_pool, pools, "bgp_originators")
            params["host_name"] = _choice(
                rng, advertise_pool, _first(advertise_pool) or router0
            )

    elif problem in _VICTIM_HOST_PROBLEMS:
        params.update(
            _access_router_victim(
                ctx, prefer_site_edge=problem == "host_static_blackhole"
            )
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
                    topology=isp_topo_from_scenario(scenario),
                    igp=str(isp_opts.get("igp") or "ospf"),  # type: ignore[arg-type]
                )
            )
            bgp = compile_bgp_plan(
                isp_plan, str(isp_opts.get("bgp_mode") or "ebgp"), rpki=True
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

    elif problem == "bgp_blackhole_community_leak":
        from nika.net_env.isp.inject_targets import isp_inject_params

        inventory = getattr(net_env, "inventory", None)
        bgp_inv = inventory.get("bgp") if isinstance(inventory, dict) else None
        if not isinstance(bgp_inv, dict) or not bgp_inv.get("rtbh"):
            from nika.net_env.isp.bgp import compile_bgp_plan
            from nika.net_env.isp.igp import IspConfig, compile_isp_plan

            isp_plan = compile_isp_plan(
                IspConfig(  # type: ignore[arg-type]
                    topology=isp_topo_from_scenario(scenario), igp="ospf"
                )
            )
            bgp = compile_bgp_plan(isp_plan, "ebgp", rtbh=True)
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
                    topology=isp_topo_from_scenario(scenario),
                    igp=str(isp_opts.get("igp") or "ospf"),  # type: ignore[arg-type]
                )
            )
            bgp = compile_bgp_plan(isp_plan, str(isp_opts.get("bgp_mode") or "ebgp"))
            assert bgp is not None
            bgp_inv = bgp.inventory
        params.update(first_ebgp_session(bgp_inv))
        params["flood_count"] = "120"

    elif problem in {"arp_acl_block", "icmp_acl_block", "http_acl_block"}:
        if problem == "arp_acl_block":
            params["host_name"] = _arp_l2_endpoint_host(pools, fallback=host0)
        elif scenario == "enterprise_branch":
            params["host_name"] = _prefer_named(
                host_pool, "br1_corp_pc", _first(host_pool) or host0
            )
        elif scenario == "k8s_lab":
            params["host_name"] = _prefer_named(host_pool, "client", host0)
        elif scenario in {"p4_dc_fabric", "sdn_l3_clos"}:
            params["host_name"] = _prefer_named(host_pool, "client_1_1", host0)
        else:
            params["host_name"] = host0

    elif problem == "dns_port_blocked":
        params["host_name"] = dns0

    elif problem == "mac_address_conflict":
        a, b = _mac_conflict_pair(net_env, rng)
        params["host_name"] = a
        params["host_name_2"] = b

    elif problem == "p4runtime_pipeline_mismatch":
        if scenario == "p4_dc_fabric":
            model = net_env.model
            observer = model.client_endpoints()[0]
            params["host_name"] = f"leaf_{observer.leaf_id}"
        elif scenario == "p4_dc_gateway":
            params["host_name"] = _prefer_prefixed_node(
                bmv2, prefix="gateway_", preferred="gateway_1", fallback="gateway_1"
            )
        else:
            leaves = _bmv2_leaves(bmv2)
            params["host_name"] = _choice(rng, leaves, "leaf_1")

    elif problem in {
        "p4_table_entry_missing",
        "p4_table_entry_misconfig",
        "bmv2_switch_down",
        "p4_action_selector_member_misconfig",
        "p4_ecmp_group_member_missing",
        "p4runtime_partial_write",
        "p4_table_resource_exhaustion",
    }:
        if problem == "p4runtime_partial_write" and scenario == "p4_dc_gateway":
            from nika.problems.forwarding_encapsulation_policy.p4runtime_helpers import (
                gateway_backend_leaf,
            )

            params["host_name"] = gateway_backend_leaf(net_env.model)
        elif problem == "p4runtime_partial_write" and scenario == "p4_dc_fabric":
            params["host_name"] = _prefer_prefixed_node(
                bmv2, prefix="leaf_", preferred="leaf_1", fallback="leaf_1"
            )
        elif problem in {
            "p4_action_selector_member_misconfig",
            "p4_ecmp_group_member_missing",
            "p4_table_resource_exhaustion",
        }:
            leaves = _bmv2_leaves(bmv2)
            params["host_name"] = _choice(rng, leaves, "leaf_1")
        elif problem == "bmv2_switch_down" and scenario == "p4_dc_fabric":
            params["host_name"] = _prefer_prefixed_node(
                bmv2, prefix="leaf_", preferred="leaf_1", fallback="leaf_1"
            )
        elif problem == "bmv2_switch_down" and scenario == "p4_dc_gateway":
            model = net_env.model
            client = model.clients[0]
            params["host_name"] = client.attached_switch
        elif (
            problem in {"p4_table_entry_missing", "p4_table_entry_misconfig"}
            and scenario == "p4_dc_fabric"
        ):
            model = net_env.model
            observer = model.client_endpoints()[0]
            victim = next(
                web for web in model.web_endpoints() if web.leaf_id != observer.leaf_id
            )
            params.update(
                host_name=f"leaf_{observer.leaf_id}",
                observer_device=observer.name,
                probe_dst_ip=victim.ip,
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
                attack_url="http://10.0.20.2/archive/",
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
        dns_list = list(servers.get("dns") or [])
        if scenario == "dc_clos":
            dns_target = _prefer_named(dns_list, "dns_pod0", dns0)
        elif scenario == "campus_lan":
            dns_target = _prefer_named(dns_list, "dns_server", dns0)
        else:
            dns_target = dns0 if dns0 in _all_device_names(net_env) else host0
        params["host_name"] = dns_target
        params["intf_name"] = _choice_interface(rng, net_env, dns_target, backend)
        params["delay_ms"] = "1000"

    elif problem == "incast_traffic_network_limitation":
        # Pin inject host + probe_dst_ip to one ICMP-reachable HTTP server on
        # the default probe path (same alignment pattern as web_dos_attack).
        real_web = list(servers.get("web") or [])
        if scenario == "enterprise_branch":
            server_pool = [h for h in (pools.get("hosts") or []) if h.endswith("_srv")]
            target = _prefer_named(
                server_pool,
                "hq_srv",
                _first(real_web) or web0 or host0,
            )
            params.update(
                host_name=target,
                probe_dst_ip="10.0.20.2",
                observer_device="br1_corp_pc",
            )
        elif scenario == "dc_clos":
            params.update(
                host_name=_prefer_named(
                    real_web,
                    "webserver0_pod0",
                    _first(real_web) or web0 or host0,
                ),
                probe_dst_ip="10.0.1.2",
                observer_device="client_0",
            )
        elif scenario == "campus_lan":
            target = _prefer_named(
                real_web,
                "web_server_0",
                _first(real_web) or web0 or host0,
            )
            params.update(
                host_name=target,
                probe_dst_ip="10.200.0.3",
                observer_device="pc_1_1_1_1",
            )
        elif scenario in {"sdn_l3_clos", "p4_dc_fabric"}:
            model = net_env.model
            observer = model.client_endpoints()[0]
            victim = next(
                web for web in model.web_endpoints() if web.leaf_id != observer.leaf_id
            )
            params.update(
                host_name=victim.name,
                probe_dst_ip=victim.ip,
                observer_device=observer.name,
            )
        elif scenario == "p4_dc_gateway":
            model = net_env.model
            victim = model.backend_pool[0]
            observer = model.clients[0]
            params.update(
                host_name=victim.name,
                probe_dst_ip=victim.ip,
                observer_device=observer.name,
            )

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
            params["client_host"] = (
                "br1_corp_pc" if "br1_corp_pc" in host_pool else host0
            )
            params["dst_ip"] = "10.0.20.2"
            params["small_url"] = "http://10.0.20.2/small.bin"
            params["large_url"] = "http://10.0.20.2/large.bin"
            params["cpu_quota"] = "0.05"
            params["stress_cpus"] = "16"
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
        elif scenario == "campus_lan":
            params["host_name"] = (
                "web_server_0"
                if "web_server_0" in web_pool
                else (web0 if web0 in web_pool else (_first(web_pool) or host0))
            )
            params["client_host"] = "pc_1_1_1_1" if "pc_1_1_1_1" in host_pool else host0
            params["dst_ip"] = "10.200.0.3"
            params["small_url"] = "http://10.200.0.3/small.bin"
            params["large_url"] = "http://10.200.0.3/large.bin"
            params["cpu_quota"] = "0.05"
            params["stress_cpus"] = "16"
        elif scenario == "llmd_lab":
            params["host_name"] = (
                "web" if "web" in web_pool else (_first(web_pool) or host0)
            )
            params["client_host"] = "client" if "client" in host_pool else host0
            params["dst_ip"] = "200.0.0.8"
            params["small_url"] = "http://200.0.0.8/small.bin"
            params["large_url"] = "http://200.0.0.8/large.bin"
            params["cpu_quota"] = "0.05"
            params["stress_cpus"] = "16"
        elif scenario in {"p4_dc_fabric", "sdn_l3_clos"}:
            model = getattr(net_env, "model", None)
            webs = list(getattr(model, "web_endpoints", lambda: [])()) if model else []
            clients = (
                list(getattr(model, "client_endpoints", lambda: [])()) if model else []
            )
            web = next(
                (w for w in webs if w.name == (params.get("host_name") or "")),
                webs[0] if webs else None,
            )
            if web is None:
                params["host_name"] = (
                    web0 if web0 in web_pool else (_first(web_pool) or host0)
                )
            else:
                client = next(
                    (c for c in clients if c.leaf_id != web.leaf_id),
                    clients[0] if clients else None,
                )
                params["host_name"] = web.name
                if client is not None:
                    params["client_host"] = client.name
                params["dst_ip"] = web.ip
                params["small_url"] = f"http://{web.ip}/small.bin"
                params["large_url"] = f"http://{web.ip}/large.bin"
                params["cpu_quota"] = "0.05"
                params["stress_cpus"] = "16"
        else:
            params["host_name"] = (
                web0 if web0 in web_pool else (_first(web_pool) or host0)
            )
        params["duration"] = "900" if scenario == "enterprise_branch" else "600"

    elif problem in {"k8s_clusterip_routing_broken", "k8s_worker_apiserver_partition"}:
        k8s_nodes = pools.get("k8s_nodes") or []
        control = _k8s_control_node(ctx)
        workers = sorted(node for node in k8s_nodes if node != control)
        params["control_node"] = control
        if problem == "k8s_worker_apiserver_partition":
            # Never fall back to the control plane: that inject is rejected at runtime.
            if not workers:
                raise ValueError(
                    "k8s_worker_apiserver_partition requires a worker node; "
                    f"{scenario} has only control-plane device {control!r}"
                )
            params["node_name"] = rng.choice(workers)
        else:
            params["node_name"] = _choice(rng, workers, control)

    elif problem == "k8s_coredns_isolated":
        k8s_nodes = pools.get("k8s_nodes") or []
        control = _k8s_control_node(ctx)
        # node_name is intentionally left unset: the fault resolves the nodes
        # actually hosting CoreDNS at inject time, which is where isolating it
        # takes the whole cluster's name resolution down.
        params["control_node"] = control
        workers = sorted(node for node in k8s_nodes if node != control)
        params["symptom_host"] = workers[0] if workers else control

    elif problem == "k8s_networkpolicy_deny":
        params["control_node"] = _k8s_control_node(ctx)
        params["symptom_host"] = _first(pools.get("hosts")) or host0
        if scenario == "llmd_lab":
            params["namespace"] = "llm-d"
            params["pod_selector"] = (
                "gateway.networking.k8s.io/gateway-name=llm-d-gateway"
            )
            params["symptom_url"] = "http://llmd/v1/models"
            params["control_url"] = "http://200.0.0.8/"
        else:
            params["namespace"] = "word-ns"
            params["pod_selector"] = "app=word"
            params["symptom_url"] = "http://datacenter.com/word"
            params["control_url"] = "http://datacenter.com/weather?location=London"

    elif problem == "load_balancer_overload":
        # Fixed capacity + HTTP surge on campus_lan NGINX VIP (web99.local).
        # Workload knobs are fixed for reproducibility (not searched at runtime).
        pc_pool = [h for h in host_pool if h.startswith("pc_")] or list(host_pool)
        probe_client = _prefer_named(
            pc_pool, "pc_1_1_1_1", _choice(rng, pc_pool, host0)
        )
        load_candidates = [h for h in pc_pool if h != probe_client]
        if not load_candidates:
            load_candidates = [probe_client]
        # One load client, distinct from the probe PC when possible.
        load_hosts = []
        for preferred in ("pc_2_1_1_1", "pc_3_1_1_1", "pc_1_2_1_1"):
            if preferred in load_candidates:
                load_hosts.append(preferred)
                break
        if not load_hosts and load_candidates:
            load_hosts = [load_candidates[0]]
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

    return params


def validate_benchmark_case(
    scenario: str,
    problem: str,
    inject: dict[str, str],
    topo_size: str = "",
    *,
    isp_options: dict[str, str] | None = None,
    net_env=None,
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

    problem_cls = problems[problem]
    if not problem_cls.is_compatible(canonical):
        compatible_columns = problem_cls.COMPATIBLE_COLUMNS
        raise ValueError(
            f"Incompatible {problem} on {scenario}: problem tags "
            f"{sorted(problem_cls.TAGS)}, scenario tags {sorted(registered_tags)}, "
            f"compatible columns "
            f"{sorted(compatible_columns) if compatible_columns is not None else 'any'}"
        )

    if net_env is None:
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
    tunnel_iface = (
        problem_cls.BENCHMARK_TUNNEL_IFACE or problem in _WG_TUNNEL_IFACE_PROBLEMS
    )
    # Tunnel ifaces are not Kathara L2 link endpoints; failures validate them.
    if host_name and intf_name and not tunnel_iface:
        device_ifaces = ifaces_by_device.get(host_name) or []
        if device_ifaces and intf_name not in device_ifaces:
            raise ValueError(
                f"Inject interface {intf_name!r} not on {host_name!r} in {scenario} "
                f"(topo_size={topo_size!r}); known interfaces: {device_ifaces}"
            )

    if (
        problem_cls.BENCHMARK_POINT_TO_POINT
        and host_name
        and intf_name
        and not tunnel_iface
    ):
        from nika.problems.rca.inventory import iter_link_termination_points

        needle = f"{host_name}:{intf_name}"
        matched = False
        for _key, tps in iter_link_termination_points(net_env):
            endpoints = [str(ep) for ep in tps]
            if needle not in endpoints:
                continue
            matched = True
            if len(endpoints) != 2:
                raise ValueError(
                    f"{problem} requires a point-to-point link at {needle} "
                    f"on {scenario} (topo_size={topo_size!r}); "
                    f"found {len(endpoints)} endpoints: {endpoints}"
                )
            break
        if not matched:
            raise ValueError(
                f"{problem} inject {needle} is not a link endpoint on {scenario} "
                f"(topo_size={topo_size!r})"
            )

    ctx = InjectValidationContext(
        scenario=scenario,
        canonical=canonical,
        topo_size=topo_size,
        isp_options=isp_options,
        net_env=net_env,
        devices=devices,
        ifaces_by_device=ifaces_by_device,
    )
    if _owns_benchmark_targets(problem_cls):
        problem_cls.validate_benchmark_inject(ctx, inject)
    else:
        _legacy_validate(problem, ctx, inject)


def _legacy_validate(
    problem: str, ctx: InjectValidationContext, inject: dict[str, str]
) -> None:
    """Name-dispatched checks for failures without ``benchmark_inject_params``."""
    scenario = ctx.scenario
    topo_size = ctx.topo_size
    net_env = ctx.net_env
    ifaces_by_device = ctx.ifaces_by_device
    host_name = inject.get("host_name")
    intf_name = inject.get("intf_name")

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
        if is_isp_scenario(scenario):
            inventory = getattr(net_env, "inventory", None)
            bgp_inv = inventory.get("bgp") if isinstance(inventory, dict) else None
            if isinstance(bgp_inv, dict) and bgp_inv.get("originated"):
                originators = sorted(
                    {
                        str(item["device"])
                        for item in bgp_inv["originated"]
                        if item.get("device")
                    }
                )
                if originators and host_name not in originators:
                    raise ValueError(
                        f"bgp_missing_route_advertisement host_name={host_name!r} is "
                        f"not a BGP originator on {scenario}; use one of: {originators}"
                    )
        else:
            _require_role_member(
                ctx,
                problem,
                host_name,
                "bgp_originators",
                reason="has no BGP network statement",
            )

    if problem in _VICTIM_HOST_PROBLEMS:
        _require_role_member(
            ctx, problem, host_name, "access_routers", reason="has no attached end host"
        )

    if problem in {"host_ip_conflict", "mac_address_conflict"}:
        _require_distinct_hosts(ctx, problem, inject)
    if problem == "flow_rule_loop":
        _require_distinct_hosts(ctx, problem, inject, switches=True)

    if problem == "dns_record_error":
        website = inject.get("target_website", "")
        domain = inject.get("target_domain", "")
        host_name = inject.get("host_name", "")
        urls = getattr(net_env, "web_urls", None) or []
        if urls:
            matched = any(
                website in url and (not domain or domain in url) for url in urls
            )
            if not matched:
                raise ValueError(
                    f"DNS record targets {website}.{domain} not found in web_urls for {scenario}: {urls}"
                )
        expected_host = _dns_host_for_domain(net_env, domain)
        if expected_host and host_name and host_name != expected_host:
            raise ValueError(
                f"DNS host {host_name!r} does not serve zone {domain!r} on {scenario}; "
                f"expected {expected_host!r}"
            )

    if problem == "k8s_worker_apiserver_partition":
        from nika.problems.support.kubernetes.base import control_node_from_net_env

        node_name = inject.get("node_name") or ""
        control = inject.get("control_node") or control_node_from_net_env(net_env) or ""
        if node_name and control and node_name == control:
            raise ValueError(
                "k8s_worker_apiserver_partition node_name must be a worker device, "
                f"not the control-plane device {control!r}"
            )


_MULTI_FAULT_COORDINATORS: dict[frozenset[str], str] = {
    frozenset(
        {"mtu_mismatch", "icmp_frag_needed_filter_misconfiguration"}
    ): "pmtud_blackhole",
    frozenset({"link_down", "host_missing_ip"}): "stacked_link_host",
    frozenset({"bgp_acl_block", "bgp_asn_misconfig"}): "bgp_acl_asn",
    frozenset({"dns_record_error", "host_incorrect_gateway"}): "dns_gateway",
}


def _coordinate_multi_inject_params(
    problems: list[str],
    params_by_problem: dict[str, dict[str, str]],
    *,
    scenario: str,
    net_env,
    rng: random.Random,
    backend: str,
) -> dict[str, dict[str, str]]:
    key = frozenset(problems)
    combo = _MULTI_FAULT_COORDINATORS.get(key)
    if combo == "pmtud_blackhole":
        mtu = dict(params_by_problem["mtu_mismatch"])
        frag = dict(params_by_problem["icmp_frag_needed_filter_misconfiguration"])
        frag["host_name"] = mtu["host_name"]
        params_by_problem["icmp_frag_needed_filter_misconfiguration"] = frag
        return params_by_problem

    if combo == "stacked_link_host":
        link = dict(params_by_problem["link_down"])
        host = dict(params_by_problem["host_missing_ip"])
        if link.get("host_name") == host.get("host_name"):
            web_hosts = list((getattr(net_env, "servers", None) or {}).get("web") or [])
            if web_hosts:
                host["host_name"] = web_hosts[0]
                ifaces = _device_interfaces(net_env).get(web_hosts[0]) or ["eth0"]
                host["intf_name"] = ifaces[0]
            else:
                hosts = list(net_env.hosts or [])
                alt = next((h for h in hosts if h != link.get("host_name")), None)
                if alt is not None:
                    host["host_name"] = alt
                    ifaces = _device_interfaces(net_env).get(alt) or ["eth0"]
                    host["intf_name"] = ifaces[0]
        params_by_problem["host_missing_ip"] = host
        return params_by_problem

    if combo == "bgp_acl_asn":
        acl = dict(params_by_problem["bgp_acl_block"])
        asn = dict(params_by_problem["bgp_asn_misconfig"])
        routers = list(net_env.routers or [])
        if acl.get("host_name") == asn.get("host_name") and len(routers) >= 2:
            asn["host_name"] = (
                routers[1] if routers[0] == acl.get("host_name") else routers[0]
            )
        params_by_problem["bgp_asn_misconfig"] = asn
        return params_by_problem

    if combo == "dns_gateway":
        dns = dict(params_by_problem["dns_record_error"])
        gw = dict(params_by_problem["host_incorrect_gateway"])
        if scenario == "campus_lan":
            dns["host_name"] = "dns_server"
            gw["host_name"] = "pc_1_1_1_1"
        elif dns.get("host_name") == gw.get("host_name"):
            hosts = list(net_env.hosts or [])
            corp = [h for h in hosts if h.endswith("_corp_pc")]
            alt = next((h for h in corp if h != dns.get("host_name")), None)
            if alt is None:
                alt = next((h for h in hosts if h != dns.get("host_name")), None)
            if alt is not None:
                gw["host_name"] = alt
        params_by_problem["dns_record_error"] = dns
        params_by_problem["host_incorrect_gateway"] = gw
        return params_by_problem

    return params_by_problem


def resolve_multi_inject_params(
    problems: list[str],
    scenario: str,
    topo_size: str = "",
    *,
    seed: int = DEFAULT_SEED,
    isp_options: dict[str, str] | None = None,
) -> dict[str, dict[str, str]]:
    """Return per-problem inject params for a coordinated multi-fault case."""
    if len(problems) < 2:
        raise ValueError("resolve_multi_inject_params requires at least two problems.")
    resolved: dict[str, dict[str, str]] = {}
    for problem in problems:
        resolved[problem] = resolve_inject_params(
            problem,
            scenario,
            topo_size,
            seed=seed,
            isp_options=isp_options,
        )
    net_env = _get_net_env_for_benchmark(scenario, topo_size, isp_options=isp_options)
    if hasattr(net_env, "load_machines") and not getattr(net_env, "hosts", None):
        net_env.load_machines()
    rng = _case_rng(
        seed,
        scenario,
        "+".join(sorted(problems)),
        topo_size,
        _isp_rng_key(isp_options),
    )
    backend = _resolve_benchmark_backend(scenario, isp_options)
    return _coordinate_multi_inject_params(
        problems,
        resolved,
        scenario=scenario,
        net_env=net_env,
        rng=rng,
        backend=backend,
    )
