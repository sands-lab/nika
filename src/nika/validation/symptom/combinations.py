"""User-facing checks for the live test of each admitted multi-fault task.

A ``joint`` combination is checked with an outage probe that neither failure
triggers alone. A ``masked`` combination lists the masked failure first: its
symptom is absent while the last failure is active and appears once that one
is recovered. ``exposed`` reads the masked symptom where the masked failure's
own symptom evaluator does not see it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from nika.validation.symptom.addressing_probes import lab_addresses
from nika.validation.symptom.path_probes import echo

Link = tuple[tuple[str, str], tuple[str, str]]


@dataclass(frozen=True)
class ProbeContext:
    """A live lab, the task's placement, and the failure instances acting on it."""

    runtime: Any
    net_env: Any
    inject: Mapping[str, Mapping[str, Any]]
    problems: Mapping[str, Any]


Probe = Callable[[ProbeContext], tuple[bool, dict[str, Any]]]


@dataclass(frozen=True)
class Check:
    mode: str  # "joint" or "masked"
    probe: Probe | None = None
    placement_valid: Callable[[Any, Mapping[str, Mapping[str, Any]]], bool] | None = (
        None
    )


# ----------------------------------------------------------------------
# Topology helpers
# ----------------------------------------------------------------------


def links(net_env: Any) -> list[Link]:
    from nika.problems.rca.inventory import (
        iter_link_termination_points,
        parse_endpoint,
    )
    from nika.telemetry import is_telemetry_link
    from nika.telemetry.management import TELEMETRY_COLLECTOR

    return [
        tuple(parse_endpoint(str(endpoint)) for endpoint in endpoints)
        for key, endpoints in iter_link_termination_points(net_env)
        if len(endpoints) == 2
        and not is_telemetry_link(key)
        and all(parse_endpoint(str(ep))[0] != TELEMETRY_COLLECTOR for ep in endpoints)
    ]


def find_link(net_env: Any, node: str, intf: str) -> Link | None:
    return next((link for link in links(net_env) if (node, intf) in link), None)


def components(nodes: Iterable[str], edges: Iterable[tuple[str, str]]) -> list[set]:
    adjacency: dict[str, set[str]] = {node: set() for node in nodes}
    for a, b in edges:
        if a in adjacency and b in adjacency:
            adjacency[a].add(b)
            adjacency[b].add(a)
    found, unseen = [], set(adjacency)
    while unseen:
        part = {unseen.pop()}
        stack = list(part)
        while stack:
            for peer in adjacency[stack.pop()] - part:
                part.add(peer)
                stack.append(peer)
        unseen -= part
        found.append(part)
    return found


def redundant_link_pair(net_env: Any, inject: Mapping[str, Mapping[str, Any]]) -> bool:
    """Each selected link preserves connectivity alone; together they cut hosts off."""
    all_links = links(net_env)
    failed = [
        find_link(net_env, inject[key]["host_name"], inject[key]["intf_name"])
        for key in ("link_down[1]", "link_down[2]")
    ]
    if None in failed or failed[0] == failed[1]:
        return False
    nodes = {node for link in all_links for node, _ in link}

    def sides(removed):
        return components(
            nodes, [(a[0], b[0]) for a, b in all_links if (a, b) not in removed]
        )

    if len(sides([])) != 1 or any(len(sides([link])) != 1 for link in failed):
        return False
    parts = sides(failed)
    hosts = set(net_env.hosts or ())
    return len(parts) == 2 and all(hosts & part for part in parts)


def _address(ctx: ProbeContext, node: str, intf: str | None = None) -> str | None:
    """An address of ``node`` on a topology data interface, not management."""
    from nika.problems.rca.inventory import kernel_data_interface

    names = (
        [intf]
        if intf
        else [i for link in links(ctx.net_env) for n, i in link if n == node]
    )
    rows = lab_addresses(ctx.runtime).get(node, [])
    if not names:
        # A host on a switched LAN has no point-to-point topology interface;
        # Kathara labs have no management network to avoid.
        return rows[0][1].split("/")[0] if rows else None
    for name in names:
        kernel = kernel_data_interface(ctx.net_env, node, name)
        for row_intf, address in rows:
            if row_intf in (kernel, name):
                return address.split("/")[0]
    return None


def partition(ctx: ProbeContext, failed: list[Link]) -> tuple[bool, dict[str, Any]]:
    """Whether an end host cut off by losing ``failed`` loses reachability."""
    all_links = links(ctx.net_env)
    nodes = {node for link in all_links for node, _ in link}
    remaining = [(a[0], b[0]) for a, b in all_links if (a, b) not in failed]
    sides = sorted(components(nodes, remaining), key=len)
    if len(sides) < 2:
        return False, {"error": "failed links do not partition the topology"}
    hosts = set(getattr(ctx.net_env, "hosts", None) or ())
    source = next((n for n in sorted(sides[0]) if n in hosts), None)
    target = next(
        (a for n in sorted(sides[1]) if n in hosts if (a := _address(ctx, n))), None
    )
    if source is None or target is None:
        return False, {"error": "no end host on each side"}
    reading = echo(ctx.runtime, source, target)
    return reading["received"] == 0, {"src": source, "dst": target, "echo": reading}


def link_pair(first: str, second: str, interface_of=None) -> Probe:
    """Partition probe over the links two failures take down."""

    def probe(ctx: ProbeContext):
        failed = []
        for name in (first, second):
            params = ctx.inject[name]
            intf = interface_of(ctx, params) if interface_of else params["intf_name"]
            failed.append(find_link(ctx.net_env, params["host_name"], intf))
        return partition(ctx, [link for link in failed if link])

    return probe


# ----------------------------------------------------------------------
# Combination-specific probes
# ----------------------------------------------------------------------


def _branch_echo(ctx: ProbeContext, edge: str, target: str):
    spoke = next(
        t.spoke for t in ctx.net_env.built_tunnels if f"{t.spoke}_edge" == edge
    )
    site = ctx.net_env.spec.sites[spoke]
    source = next(lan.host_names[0] for lan in site.lans if lan.role == "corp")
    reading = echo(ctx.runtime, source, target)
    return reading["received"] == 0, {"src": source, "dst": target, "echo": reading}


def standby_key(ctx: ProbeContext):
    edge = ctx.inject["wireguard_peer_key_misconfiguration"]["host_name"]
    return _branch_echo(ctx, edge, ctx.net_env.default_probe_path().dst_ip)


def standby_allowed_ips(ctx: ProbeContext):
    name = "wireguard_allowed_ips_misconfiguration"
    params = ctx.inject[name]
    target = ctx.problems[name]._target_host_ip(params["target_prefix"])
    return _branch_echo(ctx, params["host_name"], target)


def undersized_backup(ctx: ProbeContext):
    """Whether a transfer from the HQ server into the branch has collapsed."""
    from nika.net_env.verify import iperf_throughput_bps

    shaped = ctx.inject["link_capacity_bottleneck"]
    backup = find_link(ctx.net_env, shaped["host_name"], shaped["intf_name"]) or ()
    edges = {f"{t.spoke}_edge": t.spoke for t in ctx.net_env.built_tunnels}
    spoke = next(edges[node] for node, _ in backup if node in edges)
    site = ctx.net_env.spec.sites[spoke]
    receiver = next(lan.host_names[0] for lan in site.lans if lan.role == "corp")
    server_ip = ctx.net_env.default_probe_path().dst_ip
    sender = next(
        node
        for node, rows in lab_addresses(ctx.runtime).items()
        if any(address.split("/")[0] == server_ip for _, address in rows)
    )
    bps = iperf_throughput_bps(ctx.runtime, sender, receiver, _address(ctx, receiver))
    # A transfer that cannot complete at all has collapsed too.
    collapsed = bps is None or bps < 1_000_000
    return collapsed, {"sender": sender, "receiver": receiver, "bps": bps}


def _far_end_address(ctx: ProbeContext, router: str, intf: str) -> str | None:
    """The other end's address on ``router:intf``'s point-to-point subnet.

    Derived from the router's own address, so it also works when the far end
    is a native router whose addresses Linux does not show.
    """
    import ipaddress

    from nika.problems.rca.inventory import kernel_data_interface
    from nika.problems.support.router_ops import router_fault_ops

    ops = router_fault_ops(ctx.problems["mtu_mismatch"], router)
    if ops is not None and ops.supports("interface_addresses"):
        addresses = ops.interface_addresses(intf)
    else:
        kernel = kernel_data_interface(ctx.net_env, router, intf)
        addresses = ctx.runtime.exec(
            router, f"ip -4 -o addr show dev {kernel} scope global"
        ).split()
    for token in addresses:
        if "/" not in token:
            continue
        try:
            local = ipaddress.ip_interface(token)
        except ValueError:
            continue
        if local.version != 4 or local.network.prefixlen < 30:
            continue
        hosts = [
            address
            for address in (
                local.network
                if local.network.prefixlen == 31
                else local.network.hosts()
            )
            if address != local.ip
        ]
        if local.network.prefixlen >= 30 and len(hosts) == 1:
            return str(hosts[0])
    return None


def pmtud(ctx: ProbeContext):
    """Whether a sender behind the router loses large DF packets silently."""
    from nika.net_env.verify import ping_df_probe

    router, intf = (ctx.inject["mtu_mismatch"][k] for k in ("host_name", "intf_name"))
    target = _far_end_address(ctx, router, intf)
    if target is None:
        raise AssertionError(f"No point-to-point address on {router}:{intf}")
    hosts = set(getattr(ctx.net_env, "hosts", None) or ())
    senders = sorted(
        node
        for item in links(ctx.net_env)
        if router in {n for n, _ in item}
        for node, _ in item
        if node in hosts
    )
    # A sender that learned the smaller path MTU no longer needs Frag Needed.
    fresh = [
        h for h in senders if "mtu" not in ctx.runtime.exec(h, f"ip route get {target}")
    ]
    sender = (fresh or senders)[0]
    small_ok, _, small = ping_df_probe(ctx.runtime, sender, target, packet_size=64)
    large_ok, saw_frag, large = ping_df_probe(
        ctx.runtime, sender, target, packet_size=1400
    )
    return small_ok and not large_ok and not saw_frag, {
        "src": sender,
        "dst": target,
        "small_ok": small_ok,
        "large_ok": large_ok,
        "saw_frag_needed": saw_frag,
        "small": small,
        "large": large,
    }


def rov_gap(ctx: ProbeContext):
    """Whether the ROV router holds a leaked path for a leak prefix."""
    import json

    bgp = (getattr(ctx.net_env, "inventory", None) or {}).get("bgp") or {}
    router = ctx.inject["bgp_rov_not_enforced"].get("host_name") or bgp["rov_observer"]
    leaker = str(bgp.get("leaker_asn") or "")
    held = {}
    for prefix in bgp.get("leak_prefixes") or ():
        out = ctx.runtime.exec(
            router,
            f"vtysh -c 'show bgp ipv4 unicast {prefix} json' 2>/dev/null || true",
        )
        try:
            paths = (json.loads(out or "{}") or {}).get("paths") or []
        except ValueError:
            paths = []
        held[prefix] = any(
            str((p.get("aspath") or {}).get("string") or "").split()[-1:] == [leaker]
            for p in paths
        )
    return any(held.values()), {"router": router, "leaked_paths": held}


def prefix_surge(ctx: ProbeContext):
    name = "bgp_max_prefix_limit_low"
    problem = ctx.problems[name]
    receiver, peer, neighbor = problem._resolve_session(
        problem.parse_params(ctx.inject[name])
    )
    site = problem.customer_route(receiver, peer)
    if site is None:
        return False, {"error": "no customer-route dependency"}
    down = not problem._session_established(receiver, neighbor)
    held = (
        "peer had exceeded the max. no. of prefixes"
        in problem._neighbor_output(receiver, neighbor).lower()
    )
    missing = not problem._has_route(receiver, site["prefix"])
    reading = echo(ctx.runtime, site["host"], site["service_address"])
    return down and held and missing and reading["received"] == 0, {
        "session_down": down,
        "held_for_prefix_limit": held,
        "customer_route_missing": missing,
        "echo": reading,
    }


def _policy_interface(ctx: ProbeContext, params) -> str:
    from nika.problems.routing_control_plane.bgp import BGPSessionPolicyMissing

    interfaces = BGPSessionPolicyMissing.session_interfaces(
        ctx.net_env, params["host_name"]
    )
    return interfaces[params["peer"]]


def ingress_exposed(ctx: ProbeContext):
    """Whether a client of the Ingress gets a gateway error for the Service."""
    from nika.problems.support.kubernetes.objects import ingress_backends
    from nika.validation.symptom.k8s_probes import _client, _http_code

    problem = ctx.problems["k8s_service_target_port_mismatch"]
    namespace, _, service = (problem.service_ref or "").partition("/")
    client = _client(problem)
    codes = {
        f"http://{host}{path}": _http_code(problem, client, f"http://{host}{path}")
        for ns, name, host, path in ingress_backends(
            problem.runtime.lab_api, problem.control_node()
        )
        if (ns, name) == (namespace, service)
    }
    gateway_error = bool(codes) and all(
        code.isdigit() and 500 <= int(code) < 600 for code in codes.values()
    )
    return gateway_error, {"client": client, "codes": codes}


CHECKS: dict[str, Check] = {
    "bgp_transport_masks_asn": Check("masked"),
    "bottleneck_masks_receive_window": Check("masked"),
    "ingress_class_masks_target_port": Check("masked", ingress_exposed),
    "latent_adjacency_cut": Check(
        "joint", link_pair("ospf_hello_interval_mismatch", "link_down")
    ),
    "latent_network_type_cut": Check(
        "joint", link_pair("ospf_network_type_mismatch", "link_down")
    ),
    "pmtud_black_hole": Check("joint", pmtud),
    "policy_gap_exposed_by_uplink_loss": Check(
        "joint",
        link_pair(
            "bgp_session_policy_missing",
            "link_down",
            lambda ctx, params: (
                _policy_interface(ctx, params)
                if "peer" in params
                else params["intf_name"]
            ),
        ),
    ),
    "prefix_surge_trips_low_limit": Check("joint", prefix_surge),
    "redundant_links_both_lost": Check(
        "joint", link_pair("link_down[1]", "link_down[2]"), redundant_link_pair
    ),
    "rov_gap_admits_hijack": Check("joint", rov_gap),
    "selector_masks_readiness": Check("masked"),
    "standby_tunnel_allowed_ips_exposed": Check("joint", standby_allowed_ips),
    "standby_tunnel_key_exposed": Check("joint", standby_key),
    "undersized_backup_exposed": Check("joint", undersized_backup),
}
