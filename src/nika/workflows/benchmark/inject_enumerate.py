"""Semantic inject-target enumeration built on the canonical resolver."""

from __future__ import annotations

from nika.problems.registry import list_avail_problem_instances
from nika.problems.support.benchmark_targets import (
    InjectTargetContext,
    p4_port_options as _p4_port_options,
    replace as _replace,
    resource as _resource,
    role_nodes as _role_nodes,
    unique as _unique,
)
from nika.workflows.benchmark.inject_resolve import (
    DEFAULT_SEED,
    _align_dns_record_inject,
    _dscp_remark_targets,
    _get_net_env_for_benchmark,
    _load_inventory,
    _owns_benchmark_targets,
    _prefer_hq_server_prefix,
    _primary_hq_wg_targets,
    _remote_prefixes_for_spoke,
    _resolve,
    _role_subset,
)

_CANONICAL_ONLY = frozenset(
    {
        "bgp_blackhole_community_leak",
        "bgp_max_prefix_exceeded",
        "bgp_rpki_invalid_route_leak",
        "device_forwarding_packet_corruption",
        "icmp_frag_needed_filter_misconfiguration",
        "incast_traffic_network_limitation",
        "k8s_coredns_isolated",
        "k8s_networkpolicy_deny",
        "lb_connection_state_exhaustion",
        "lb_pending_connection_update_race",
        "load_balancer_overload",
        "mtu_mismatch",
        "nat_mapping_removed_without_drain",
        "p4_tcam_entry_corruption",
        "sender_resource_contention",
        "snat_port_pool_exhaustion",
        "web_dos_attack",
        "dns_lookup_latency",
    }
)

_P4_PORT_TARGETS = frozenset(
    {
        "int_insufficient_mtu_headroom",
        "p4_ecn_threshold_misconfiguration",
    }
)


def _node_options(
    base: dict[str, str], problem: str, scenario: str, resource, net_env
) -> list[dict[str, str]]:
    node = str(resource.node or "")
    field = next(
        (
            key
            for key in (
                "host_name",
                "host_name_2",
                "forwarding_device",
                "attacker_device",
                "node_name",
            )
            if base.get(key) == node
        ),
        None,
    )
    if field is None:
        return [base]
    targets = _role_nodes(net_env, node)
    if problem == "bgp_missing_route_advertisement":
        targets = _role_subset(targets, net_env.target_roles(), "bgp_originators")
    elif problem in {"bgp_hijacking", "host_static_blackhole"}:
        targets = _role_subset(targets, net_env.target_roles(), "access_routers")
    elif problem == "k8s_worker_apiserver_partition":
        # Partitioning the control plane from itself is unverifiable.
        from nika.problems.support.kubernetes.base import control_node_from_net_env

        control = control_node_from_net_env(net_env)
        if control:
            targets = [target for target in targets if target != control]
    rows: list[dict[str, str]] = []
    for target in targets:
        row = _replace(base, **{field: target})
        other = "host_name_2" if field == "host_name" else "host_name"
        if field in {"host_name", "host_name_2"} and row.get(other) == target:
            donor = next(
                (item for item in _role_nodes(net_env, node) if item != target), None
            )
            if donor is None:
                continue
            row[other] = donor
        if problem == "dns_record_error" and field == "host_name":
            row = _align_dns_record_inject(net_env, row)
        rows.append(row)
    return rows


def _compound_options(
    base: dict[str, str], ctx: InjectTargetContext
) -> list[dict[str, str]] | None:
    problem, scenario = ctx.problem, ctx.scenario
    topo_size, net_env = ctx.topo_size, ctx.net_env
    from nika.workflows.benchmark.isp_options import is_isp_scenario

    if problem == "bgp_missing_route_advertisement" and is_isp_scenario(scenario):
        from nika.net_env.isp.inject_targets import enrich_isp_symptom_params

        inventory = getattr(net_env, "inventory", None) or {}
        bgp = inventory.get("bgp") or {}
        rows = []
        for item in sorted(
            bgp.get("originated") or [],
            key=lambda row: (
                str(row.get("device") or ""),
                str(row.get("prefix") or ""),
            ),
        ):
            host = str(item.get("device") or "")
            prefix = str(item.get("prefix") or "")
            if not host:
                continue
            row = _replace(base, host_name=host)
            if prefix:
                row["prefix"] = prefix
            for key in ("symptom_host", "probe_dst_ip", "peer_host"):
                row.pop(key, None)
            enrich_isp_symptom_params(row, problem, inventory, bgp)
            rows.append(row)
        return rows
    if problem == "wireguard_peer_key_misconfiguration":
        return [
            _replace(base, host_name=node, intf_name=intf)
            for node, intf in _primary_hq_wg_targets(topo_size)
        ]
    if problem == "wireguard_allowed_ips_misconfiguration":
        rows = []
        for node, intf in _primary_hq_wg_targets(topo_size):
            prefix = _prefer_hq_server_prefix(
                _remote_prefixes_for_spoke(topo_size, node.removesuffix("_edge"))
            )
            if prefix:
                rows.append(
                    _replace(base, host_name=node, intf_name=intf, target_prefix=prefix)
                )
        return rows
    if problem == "vrf_dscp_remarking":
        return [
            _replace(
                base,
                host_name=target.edge,
                intf_name=target.intf_name,
                src_host=target.src_host,
                dst_host=target.dst_host,
                corp_prefix=target.corp_prefix,
            )
            for target in _dscp_remark_targets(topo_size)
        ]
    if problem in _P4_PORT_TARGETS:
        return _p4_port_options(
            ctx, base, gateways_only=problem == "int_insufficient_mtu_headroom"
        )
    return None


def enumerate_inject_params(
    problem: str,
    scenario: str,
    topo_size: str = "",
    *,
    isp_options: dict[str, str] | None = None,
    net_env=None,
) -> list[dict[str, str]]:
    """Return legal target variants while retaining canonical auxiliary knobs."""
    if net_env is None:
        net_env = _get_net_env_for_benchmark(
            scenario, topo_size, isp_options=isp_options
        )
    _load_inventory(net_env)
    base, ctx = _resolve(
        problem,
        scenario,
        topo_size,
        seed=DEFAULT_SEED,
        isp_options=isp_options,
        net_env=net_env,
    )
    problem_cls = list_avail_problem_instances().get(problem)
    if _owns_benchmark_targets(problem_cls):
        return _unique(problem_cls.benchmark_inject_options(ctx, base))
    return _legacy_inject_options(base, ctx)


def _legacy_inject_options(
    base: dict[str, str], ctx: InjectTargetContext
) -> list[dict[str, str]]:
    """Name-dispatched variants for failures without ``benchmark_inject_params``."""
    problem, scenario = ctx.problem, ctx.scenario
    net_env = ctx.net_env
    compound = _compound_options(base, ctx)
    if compound is not None:
        return _unique(compound)
    if problem in _CANONICAL_ONLY:
        return [base]
    if (
        problem in {"p4_table_entry_missing", "p4_table_entry_misconfig"}
        and scenario == "p4_dc_fabric"
    ):
        # Off-path leaves do not forward the default client_1_1 HTTP probe.
        return [base]
    resource = _resource(ctx, base)
    if resource is None:
        return [base]
    if str(resource.kind) == "node":
        return _unique(_node_options(base, problem, scenario, resource, net_env))
    return [base]
