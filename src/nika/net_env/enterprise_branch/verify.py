"""Healthy-state verification for enterprise_branch."""

from __future__ import annotations

from typing import Any

from nika.net_env.enterprise_branch.addressing import (
    edge_name_for,
    isp_name_for,
    vrf_name,
)
from nika.net_env.enterprise_branch.topology import (
    LOCAL_ONLY_ROLES,
    BuiltTunnel,
    LanSpec,
    TopoSize,
    TopoSpec,
    build_topo_spec,
    overlay_qos_for,
)
from nika.net_env.enterprise_branch.edge_view import FrrEdgeView
from nika.net_env.verify import (
    build_lab_verify_result,
    exec_or_empty,
    host_has_ipv4,
    http_ok,
    nodes_deployed,
    ping_ok,
)

from nika.utils.parallel import bounded_parallel_map
from nika.runtime.base import LabRuntime
from nika.net_env.verify_collection import (
    VerifyCommand,
    collect_verify_commands,
    ping_command,
    http_command,
)

FRR_EDGES = FrrEdgeView()


def _lan_host_ip(lan: LanSpec, host_index: int = 0) -> str:
    """IPv4 of a LAN host; default is the primary (.2) endpoint."""
    # prefix is 10.x.y.0/24 → host N is .2+N
    base = lan.prefix.split("/")[0]  # 10.x.y.0
    parts = base.split(".")
    parts[-1] = str(2 + host_index)
    return ".".join(parts)


def _corp_lan(spec: TopoSpec, site: str) -> LanSpec:
    return next(lan for lan in spec.sites[site].lans if lan.role == "corp")


def _server_lan(spec: TopoSpec, site: str) -> LanSpec:
    return next(lan for lan in spec.sites[site].lans if lan.role == "server")


def _prefix_net(prefix: str) -> str:
    """Return network address without mask (FRR show ip bgp match)."""
    return prefix.split("/")[0]


def _rib_has_prefix(rib: str, prefix: str) -> bool:
    net = _prefix_net(prefix)
    return net in rib


def _collect(runtime, spec, tunnels, view, *, full):
    commands = []

    def add(node, command, timeout=20):
        commands.append(VerifyCommand(node, command, timeout))

    def ping(node, target):
        add(node, ping_command(target), 15)

    hq_corp = _corp_lan(spec, "hq")
    hq_srv = _server_lan(spec, "hq")
    hq_ip, srv_ip = _lan_host_ip(hq_corp), _lan_host_ip(hq_srv)
    if not full:
        add(hq_corp.host_name, "ip -4 -o addr show dev eth0", 10)
        add(edge_name_for("hq"), view.routing_process, 10)
        for spoke in spec.branch_names():
            add(edge_name_for(spoke), view.bgp_rib)
            ping(_corp_lan(spec, spoke).host_name, srv_ip)
        return collect_verify_commands(runtime, commands)
    for site in spec.sites.values():
        add(edge_name_for(site.name), view.vrf_list, 15)
        for lan in site.lans:
            for host in lan.host_names:
                add(host, "ip -4 -o addr show dev eth0", 10)
            if lan.role in LOCAL_ONLY_ROLES:
                remote = (
                    _corp_lan(spec, spec.branch_names()[0])
                    if site.name == "hq" and spec.branch_names()
                    else hq_corp
                )
                for host in lan.host_names:
                    ping(host, _lan_host_ip(remote))
                    ping(host, _lan_host_ip(_corp_lan(spec, site.name)))
    seen = set()
    for tunnel in tunnels:
        left, right = edge_name_for(tunnel.spoke), edge_name_for(tunnel.hub)
        add(left, view.ping(tunnel.hub_wan_ip), 15)
        add(right, view.ping(tunnel.spoke_wan_ip), 15)
        for edge, interface in ((left, tunnel.spoke_iface), (right, tunnel.hub_iface)):
            add(edge, view.wireguard(interface), 15)
            add(edge, view.bgp_peers.command)
            if (edge, interface) not in seen:
                seen.add((edge, interface))
                for command in view.qos(interface):
                    add(edge, command, 15)
        if not tunnel.primary and tunnel.spoke in spec.branch_names():
            add(left, view.bgp_route(hq_corp.prefix))
    for spoke in spec.branch_names():
        edge, corp = edge_name_for(spoke), _corp_lan(spec, spoke)
        add(edge, view.bgp_rib)
        for command in view.vrf_ribs(vrf_name("corp"), routing_table_first=True):
            add(edge, command, 15)
        for lan in spec.sites[spoke].lans:
            if lan.role in LOCAL_ONLY_ROLES:
                for command in view.vrf_ribs(lan.vrf, routing_table_first=False):
                    add(edge, command, 15)
        ping(corp.host_name, hq_ip)
        ping(hq_corp.host_name, _lan_host_ip(corp))
        add(corp.host_name, http_command(f"http://{srv_ip}/"))
    dc2 = _corp_lan(spec, "dc2")
    ping(hq_corp.host_name, _lan_host_ip(dc2))
    ping(dc2.host_name, hq_ip)
    add(dc2.host_name, http_command(f"http://{srv_ip}/"))
    spokes = spec.branch_names()
    for i, source in enumerate(spokes):
        for destination in spokes[i + 1 :]:
            target_lan = _corp_lan(spec, destination)
            target = _lan_host_ip(target_lan)
            ping(_corp_lan(spec, source).host_name, target)
            add(
                edge_name_for(source),
                view.route_lookup(vrf_name("corp"), target, target_lan.prefix),
                15,
            )
    for provider in spec.providers:
        add(isp_name_for(provider), "ip route", 15)
        add(isp_name_for(provider), "vtysh -c 'show ip route'", 15)
    return collect_verify_commands(runtime, commands)


def verify_enterprise_branch_lab_startup(
    runtime: LabRuntime,
    *,
    scenario_name: str,
    topo_size: TopoSize,
    built_tunnels: list[BuiltTunnel] | None = None,
    spec: TopoSpec | None = None,
    edge_view: FrrEdgeView = FRR_EDGES,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Bounded readiness: nodes, HQ edge routing, and every branch overlay to HQ."""
    spec = spec or build_topo_spec(topo_size)
    view = edge_view
    runtime = _collect(runtime, spec, list(built_tunnels or []), view, full=False)
    hq_corp = _corp_lan(spec, "hq")
    hq_srv = _server_lan(spec, "hq")
    hq_srv_ip = _lan_host_ip(hq_srv)
    checks = {
        "nodes_deployed": nodes_deployed(runtime, spec.all_node_names()),
        "hq_corp_ipv4": host_has_ipv4(runtime, hq_corp.host_name, "10.0.10.2"),
        "hq_edge_frr": view.routing_process_ok(
            edge_name_for("hq"),
            exec_or_empty(
                runtime, edge_name_for("hq"), view.routing_process, timeout=10
            ),
        ),
    }

    def spoke_overlay_checks(spoke: str) -> dict[str, bool]:
        rib = exec_or_empty(runtime, edge_name_for(spoke), view.bgp_rib, timeout=20)
        corp = _corp_lan(spec, spoke)
        return {
            f"overlay_hq_corp_on_{spoke}": _rib_has_prefix(rib, hq_corp.prefix),
            f"overlay_hq_server_on_{spoke}": _rib_has_prefix(rib, hq_srv.prefix),
            f"e2e_{spoke}_to_hq_server": ping_ok(runtime, corp.host_name, hq_srv_ip),
        }

    for spoke_result in bounded_parallel_map(spoke_overlay_checks, spec.branch_names()):
        checks.update(spoke_result)
    return runtime.finish(
        build_lab_verify_result(
            scenario_name=scenario_name,
            verified=all(checks.values()),
            checks=checks,
            details={"startup_check": True, "topo_size": topo_size},
        )
    )


def verify_enterprise_branch_lab(
    runtime: LabRuntime,
    *,
    scenario_name: str,
    topo_size: TopoSize,
    built_tunnels: list[BuiltTunnel] | None = None,
    spec: TopoSpec | None = None,
    edge_view: FrrEdgeView = FRR_EDGES,
    **_kwargs: Any,
) -> dict[str, Any]:
    spec = spec or build_topo_spec(topo_size)
    view = edge_view
    runtime = _collect(runtime, spec, list(built_tunnels or []), view, full=True)
    checks: dict[str, bool] = {}
    details: dict[str, Any] = {"topo_size": topo_size}
    tunnels = list(built_tunnels or [])

    checks["nodes_deployed"] = nodes_deployed(runtime, spec.all_node_names())

    # --- Local addressing: every business host ---
    address_jobs = [
        (f"host_ipv4_{host_name}", host_name, _lan_host_ip(lan, host_index))
        for site in spec.sites.values()
        for lan in site.lans
        for host_index, host_name in enumerate(lan.host_names)
    ]
    for key, value in bounded_parallel_map(
        lambda job: (job[0], host_has_ipv4(runtime, job[1], job[2])),
        address_jobs,
    ):
        checks[key] = value

    # --- VRF devices present on every Site Edge ---
    def site_vrf_checks(site) -> dict[str, bool]:
        edge = edge_name_for(site.name)
        vrf_out = exec_or_empty(runtime, edge, view.vrf_list, timeout=15)
        return {f"vrf_{site.name}_{lan.role}": lan.vrf in vrf_out for lan in site.lans}

    for site_checks in bounded_parallel_map(site_vrf_checks, spec.sites.values()):
        checks.update(site_checks)

    # --- Underlay + WG + BGP: every designed tunnel ---
    def tunnel_checks(bt: BuiltTunnel) -> dict[str, bool]:
        spoke_edge = edge_name_for(bt.spoke)
        hub_edge = edge_name_for(bt.hub)
        tag = f"{bt.spoke}_{bt.hub}_{bt.provider}_{'pri' if bt.primary else 'bak'}"
        result = {
            f"underlay_{tag}_spoke_to_hub": view.ping_ok(
                runtime, spoke_edge, bt.hub_wan_ip
            ),
            f"underlay_{tag}_hub_to_spoke": view.ping_ok(
                runtime, hub_edge, bt.spoke_wan_ip
            ),
            f"wg_{tag}_spoke": bool(
                exec_or_empty(
                    runtime, spoke_edge, view.wireguard(bt.spoke_iface), timeout=15
                ).strip()
            ),
            f"wg_{tag}_hub": bool(
                exec_or_empty(
                    runtime, hub_edge, view.wireguard(bt.hub_iface), timeout=15
                ).strip()
            ),
        }

        peers = view.bgp_peers
        spoke_sum = exec_or_empty(runtime, spoke_edge, peers.command, timeout=20)
        hub_sum = exec_or_empty(runtime, hub_edge, peers.command, timeout=20)
        result[f"bgp_{tag}_spoke"] = bt.hub_tunnel_ip in peers.established_peers(
            spoke_sum
        )
        result[f"bgp_{tag}_hub"] = bt.spoke_tunnel_ip in peers.established_peers(
            hub_sum
        )
        return result

    for tunnel_result in bounded_parallel_map(tunnel_checks, tunnels):
        checks.update(tunnel_result)

    if not tunnels:
        checks["underlay_any_tunnel"] = False

    # --- Overlay RIB on every spoke: HQ CORP/SERVER present; local-only absent ---
    hq_corp = _corp_lan(spec, "hq")
    hq_srv = _server_lan(spec, "hq")
    hq_corp_net = _prefix_net(hq_corp.prefix)
    hq_srv_net = _prefix_net(hq_srv.prefix)
    local_only_nets = [_prefix_net(p) for p in spec.local_only_prefixes()]
    spokes = spec.branch_names()

    def spoke_overlay_checks(spoke: str) -> dict[str, bool]:
        result: dict[str, bool] = {}
        edge = edge_name_for(spoke)
        rib = exec_or_empty(runtime, edge, view.bgp_rib, timeout=20)
        result[f"overlay_hq_corp_on_{spoke}"] = hq_corp_net in rib
        result[f"overlay_hq_server_on_{spoke}"] = hq_srv_net in rib
        for net in local_only_nets:
            result[f"overlay_no_{net}_on_{spoke}"] = net not in rib

        # CORP VRF RIB: HQ CORP + SERVER (shared-services leak); no guest/iot.
        corp_rib = ""
        for command in view.vrf_ribs(vrf_name("corp"), routing_table_first=True):
            corp_rib = exec_or_empty(runtime, edge, command, timeout=20)
            if corp_rib.strip():
                break
        result[f"vrf_corp_hq_corp_on_{spoke}"] = _rib_has_prefix(
            corp_rib, hq_corp.prefix
        )
        result[f"vrf_corp_hq_server_on_{spoke}"] = _rib_has_prefix(
            corp_rib, hq_srv.prefix
        )
        for net in local_only_nets:
            result[f"vrf_corp_no_{net}_on_{spoke}"] = net not in corp_rib

        # Local-only VRFs must not carry remote CORP/SERVER.
        for lan in spec.sites[spoke].lans:
            if lan.role not in LOCAL_ONLY_ROLES:
                continue
            local_rib = "".join(
                exec_or_empty(runtime, edge, command, timeout=15)
                for command in view.vrf_ribs(lan.vrf, routing_table_first=False)
            )
            result[f"vrf_{lan.role}_no_hq_corp_on_{spoke}"] = (
                hq_corp_net not in local_rib
            )
            result[f"vrf_{lan.role}_no_hq_server_on_{spoke}"] = (
                hq_srv_net not in local_rib
            )
        return result

    for spoke_result in bounded_parallel_map(spoke_overlay_checks, spokes):
        checks.update(spoke_result)

    # --- E2E: every branch CORP <-> HQ CORP and HTTP to HQ SERVER ---
    hq_corp_ip = _lan_host_ip(hq_corp)
    hq_srv_ip = _lan_host_ip(hq_srv)

    def spoke_e2e_checks(spoke: str) -> dict[str, bool]:
        corp = _corp_lan(spec, spoke)
        corp_ip = _lan_host_ip(corp)
        return {
            f"e2e_{spoke}_to_hq_corp": ping_ok(runtime, corp.host_name, hq_corp_ip),
            f"e2e_hq_to_{spoke}_corp": ping_ok(runtime, hq_corp.host_name, corp_ip),
            f"e2e_{spoke}_http_hq_server": http_ok(
                runtime, corp.host_name, f"http://{hq_srv_ip}/"
            ),
        }

    for spoke_result in bounded_parallel_map(spoke_e2e_checks, spokes):
        checks.update(spoke_result)

    # --- Hub interconnect: HQ CORP <-> DC2 CORP + HTTP ---
    dc2_corp = _corp_lan(spec, "dc2")
    checks["e2e_hq_to_dc2_corp"] = ping_ok(
        runtime, hq_corp.host_name, _lan_host_ip(dc2_corp)
    )
    checks["e2e_dc2_to_hq_corp"] = ping_ok(runtime, dc2_corp.host_name, hq_corp_ip)
    checks["e2e_dc2_http_hq_server"] = http_ok(
        runtime, dc2_corp.host_name, f"http://{hq_srv_ip}/"
    )

    # --- Spoke-spoke: every pair, via overlay (corp VRF path) ---
    spoke_pairs = [(a, b) for i, a in enumerate(spokes) for b in spokes[i + 1 :]]

    def spoke_pair_checks(pair: tuple[str, str]) -> dict[str, bool]:
        a, b = pair
        a_corp = _corp_lan(spec, a)
        b_lan = _corp_lan(spec, b)
        b_ip = _lan_host_ip(b_lan)
        route_get = exec_or_empty(
            runtime,
            edge_name_for(a),
            view.route_lookup(vrf_name("corp"), b_ip, b_lan.prefix),
            timeout=15,
        )
        return {
            f"e2e_{a}_to_{b}": ping_ok(runtime, a_corp.host_name, b_ip),
            f"overlay_path_{a}_to_{b}": view.overlay_path_ok(route_get),
        }

    for pair_result in bounded_parallel_map(spoke_pair_checks, spoke_pairs):
        checks.update(pair_result)

    # --- Providers: no enterprise prefixes on any ISP ---
    def provider_checks(provider: str) -> tuple[str, bool]:
        isp = isp_name_for(provider)
        isp_routes = exec_or_empty(runtime, isp, "ip route", timeout=15)
        isp_frr = exec_or_empty(runtime, isp, "vtysh -c 'show ip route'", timeout=15)
        leaked = any(
            p.split("/")[0] in isp_routes or p.split("/")[0] in isp_frr
            for p in spec.advertised_prefixes()
        )
        return f"provider_{provider}_no_enterprise", not leaked

    for key, value in bounded_parallel_map(provider_checks, spec.providers):
        checks[key] = value

    # --- Cross-VRF isolation: guest/iot vs remote CORP; guest vs local CORP ---
    isolation_jobs: list[tuple[str, str, str]] = []
    for site in spec.sites.values():
        for lan in site.lans:
            if lan.role not in LOCAL_ONLY_ROLES:
                continue
            if site.name == "hq":
                remote_corp = _corp_lan(spec, spokes[0]) if spokes else None
            else:
                remote_corp = hq_corp
            if remote_corp is None:
                for host_name in lan.host_names:
                    checks[f"{lan.role}_{host_name}_isolated"] = True
                continue
            remote_ip = _lan_host_ip(remote_corp)
            for host_name in lan.host_names:
                isolation_jobs.append(
                    (f"{lan.role}_{host_name}_isolated", host_name, remote_ip)
                )
            # Same-site CORP must also be unreachable from local-only VRFs.
            local_corp = _corp_lan(spec, site.name)
            local_corp_ip = _lan_host_ip(local_corp)
            for host_name in lan.host_names:
                isolation_jobs.append(
                    (
                        f"{lan.role}_{host_name}_no_local_corp",
                        host_name,
                        local_corp_ip,
                    )
                )
    for key, isolated in bounded_parallel_map(
        lambda job: (
            job[0],
            not ping_ok(runtime, job[1], job[2], count=1),
        ),
        isolation_jobs,
    ):
        checks[key] = isolated

    # --- Overlay egress QoS: HTB EF/BE classes on every WireGuard iface ---
    qos = overlay_qos_for(topo_size)
    seen_qos: set[tuple[str, str]] = set()
    qos_jobs: list[tuple[str, str]] = []
    for bt in tunnels:
        for edge_name, iface in (
            (edge_name_for(bt.spoke), bt.spoke_iface),
            (edge_name_for(bt.hub), bt.hub_iface),
        ):
            key = (edge_name, iface)
            if key in seen_qos:
                continue
            seen_qos.add(key)
            qos_jobs.append(key)

    def qos_checks(job: tuple[str, str]) -> dict[str, bool]:
        edge_name, iface = job
        outputs = tuple(
            exec_or_empty(runtime, edge_name, command, timeout=15)
            for command in view.qos(iface)
        )
        tag = f"{edge_name}_{iface}"
        return {
            f"qos_{key}_{tag}": value
            for key, value in view.qos_checks(outputs, qos).items()
        }

    for qos_result in bounded_parallel_map(qos_checks, qos_jobs):
        checks.update(qos_result)

    # --- Dual-path: every backup session up; primary preferred per dual spoke ---
    backups = [t for t in tunnels if not t.primary]
    checks["backup_paths_present"] = bool(backups)

    def backup_check(bt: BuiltTunnel) -> tuple[str, bool]:
        summary = exec_or_empty(
            runtime, edge_name_for(bt.spoke), view.bgp_peers.command, timeout=20
        )
        return (
            f"backup_bgp_{bt.spoke}_{bt.hub}_{bt.provider}",
            bt.hub_tunnel_ip in view.bgp_peers.established_peers(summary),
        )

    for key, value in bounded_parallel_map(backup_check, backups):
        checks[key] = value

    dual_spokes = sorted({t.spoke for t in tunnels if not t.primary})
    hq_corp_cidr = hq_corp.prefix

    def primary_check(spoke: str) -> tuple[str, bool] | None:
        if spoke not in spokes:
            # Hub interconnect backups are checked above; primary-pref is spoke-only.
            return None
        primaries = [t for t in tunnels if t.spoke == spoke and t.primary]
        if not primaries:
            return None
        best = exec_or_empty(
            runtime, edge_name_for(spoke), view.bgp_route(hq_corp_cidr), timeout=20
        )
        return f"primary_path_{spoke}", primaries[0].hub_tunnel_ip in best

    for result in bounded_parallel_map(primary_check, dual_spokes):
        if result is not None:
            checks[result[0]] = result[1]

    details["failed"] = [k for k, v in checks.items() if not v]
    details["check_count"] = len(checks)
    return runtime.finish(
        build_lab_verify_result(
            scenario_name=scenario_name,
            verified=all(checks.values()),
            checks=checks,
            details=details,
        )
    )
