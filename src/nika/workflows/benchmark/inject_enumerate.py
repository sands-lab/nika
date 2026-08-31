"""Semantic inject-target enumeration built on the canonical resolver."""

from __future__ import annotations

from typing import Any

from nika.problems.rca.inventory import (
    iter_link_termination_points,
    parse_endpoint,
)
from nika.problems.rca.materialize import ground_truth_for_case
from nika.workflows.benchmark.inject_resolve import (
    DEFAULT_SEED,
    _device_interfaces,
    _align_dns_record_inject,
    _dscp_remark_targets,
    _get_net_env_for_benchmark,
    _load_inventory,
    _prefer_hq_server_prefix,
    _primary_hq_wg_targets,
    _routers_with_bgp_network,
    _routers_with_victim_hosts,
    _remote_prefixes_for_spoke,
    resolve_inject_params,
)

_LINK_TARGET_PROBLEMS = frozenset(
    {
        "link_down",
        "link_detach",
        "link_capacity_bottleneck",
        "link_flap",
        "link_packet_corruption",
    }
)

# Kathara dynamic VDE proxy (TBF / netem / flap) only works on 2-endpoint LANs.
_VDE_POINT_TO_POINT_PROBLEMS = frozenset(
    {
        "link_capacity_bottleneck",
        "link_flap",
        "link_packet_corruption",
    }
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
        "silent_egress_packet_loss",
    }
)


def _unique(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    by_key = {tuple(sorted(row.items())): row for row in rows}
    return [by_key[key] for key in sorted(by_key)]


def _replace(base: dict[str, str], **values: str) -> dict[str, str]:
    return {**base, **{key: str(value) for key, value in values.items()}}


def _role_nodes(net_env: Any, base_node: str) -> list[str]:
    servers = getattr(net_env, "servers", None) or {}
    for nodes in servers.values():
        if base_node in (nodes or []):
            return sorted(nodes)
    pools = (
        getattr(net_env, "kubernetes_nodes", None) or [],
        getattr(net_env, "sdn_controllers", None) or [],
        getattr(net_env, "bmv2_switches", None) or [],
        getattr(net_env, "ovs_switches", None) or [],
        getattr(net_env, "routers", None) or [],
        getattr(net_env, "hosts", None) or [],
    )
    for nodes in pools:
        if base_node not in nodes:
            continue
        if base_node.startswith(("leaf_", "gateway_", "spine_")):
            prefix = base_node.partition("_")[0] + "_"
            return sorted(node for node in nodes if node.startswith(prefix))
        return sorted(nodes)
    return [base_node]


def _resource(
    base: dict[str, str], problem: str, scenario: str, topo_size: str, net_env
):
    truth = ground_truth_for_case(
        problem=problem,
        params=base,
        scenario=scenario,
        topo_size=topo_size,
        net_env=net_env,
    )
    if len(truth.root_causes) != 1:
        return None
    return truth.root_causes[0].resource


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
        targets = _routers_with_bgp_network(targets, scenario=scenario)
    elif problem in {"bgp_hijacking", "host_static_blackhole"}:
        targets = _routers_with_victim_hosts(targets, scenario=scenario)
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


def _link_options(
    base: dict[str, str],
    net_env,
    *,
    point_to_point_only: bool = False,
) -> list[dict[str, str]]:
    interfaces = _device_interfaces(net_env)
    rows: list[dict[str, str]] = []
    for _key, raw_endpoints in iter_link_termination_points(net_env):
        parsed = sorted(parse_endpoint(str(item)) for item in raw_endpoints)
        if len(parsed) < 2:
            continue
        if point_to_point_only and len(parsed) != 2:
            continue
        eligible = [item for item in parsed if item[1] in interfaces.get(item[0], ())]
        node, intf = (eligible or parsed)[0]
        rows.append(_replace(base, host_name=node, intf_name=intf))
    return rows


def _p4_port_options(
    base: dict[str, str], problem: str, net_env
) -> list[dict[str, str]]:
    model = getattr(net_env, "model", None)
    nodes = list(getattr(model, "gateways", None) or [])
    if problem != "int_insufficient_mtu_headroom":
        nodes += list(getattr(model, "spines", None) or [])
    rows: list[dict[str, str]] = []
    for node in sorted(nodes):
        for port in sorted(
            getattr(model, "ports", {}).get(node, []), key=lambda item: item.name
        ):
            if port.role in {"spine", "leaf"}:
                rows.append(
                    _replace(
                        base,
                        host_name=node,
                        intf_name=port.name,
                        bmv2_port=str(port.bmv2_port),
                    )
                )
    return rows


def _compound_options(
    base: dict[str, str], problem: str, scenario: str, topo_size: str, net_env
) -> list[dict[str, str]] | None:
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
        return _p4_port_options(base, problem, net_env)
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
    base = resolve_inject_params(
        problem,
        scenario,
        topo_size,
        seed=DEFAULT_SEED,
        isp_options=isp_options,
        net_env=net_env,
    )
    compound = _compound_options(base, problem, scenario, topo_size, net_env)
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
    if problem in _LINK_TARGET_PROBLEMS:
        from nika.workflows.benchmark.isp_options import is_isp_scenario

        drop = {"host_name", "intf_name"}
        if is_isp_scenario(scenario):
            # Per-link observers; do not freeze canonical first-link probes.
            drop |= {"symptom_host", "probe_dst_ip", "peer_host"}
        aux = {key: value for key, value in base.items() if key not in drop}
        link_base = {
            "host_name": base["host_name"],
            "intf_name": base["intf_name"],
        }
        options = _link_options(
            link_base,
            net_env,
            point_to_point_only=problem in _VDE_POINT_TO_POINT_PROBLEMS,
        )
        # campus_lan LB backends are off the default pc→web0 ICMP probe path.
        if problem == "link_detach" and scenario == "campus_lan":
            options = [
                row
                for row in options
                if not str(row.get("host_name", "")).startswith("backend_web_")
            ]
        rows = [{**aux, **row} for row in options]
        if is_isp_scenario(scenario):
            from nika.net_env.isp.inject_targets import isp_link_symptom_targets

            inventory = getattr(net_env, "inventory", None) or {}
            enriched: list[dict[str, str]] = []
            for row in rows:
                try:
                    targets = isp_link_symptom_targets(
                        inventory, row["host_name"], row["intf_name"]
                    )
                except ValueError:
                    # Stub/edge attachments are outside inventory backbone links.
                    continue
                enriched.append({**row, **targets})
            rows = enriched
        return _unique(rows)
    resource = _resource(base, problem, scenario, topo_size, net_env)
    if resource is None:
        return [base]
    if str(resource.kind) == "node":
        return _unique(_node_options(base, problem, scenario, resource, net_env))
    return [base]
