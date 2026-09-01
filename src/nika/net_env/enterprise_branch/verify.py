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
from nika.net_env.verify import (
    bounded_parallel_map,
    build_lab_verify_result,
    exec_or_empty,
    host_has_ipv4,
    http_ok,
    nodes_deployed,
    ping_ok,
)
from nika.runtime.base import LabRuntime


def _bgp_neighbor_established(line: str) -> bool:
    """Return True when a BGP summary neighbor line shows prefixes received."""
    fields = line.split()
    if len(fields) < 10:
        return False
    # Modern FRR: Neighbor V AS MsgRcvd MsgSent TblVer InQ OutQ Up/Down State/PfxRcd ...
    return fields[9].isdigit()


def _peer_established(summary: str, peer_ip: str) -> bool:
    for line in summary.splitlines():
        if peer_ip in line and _bgp_neighbor_established(line):
            return True
    return False


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


def verify_enterprise_branch_lab_startup(
    runtime: LabRuntime,
    *,
    scenario_name: str,
    topo_size: TopoSize,
    built_tunnels: list[BuiltTunnel] | None = None,
    spec: TopoSpec | None = None,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Bounded readiness: nodes, HQ CORP attachment, and HQ edge FRR."""
    spec = spec or build_topo_spec(topo_size)
    hq_corp = _corp_lan(spec, "hq")
    checks = {
        "nodes_deployed": nodes_deployed(runtime, spec.all_node_names()),
        "hq_corp_ipv4": host_has_ipv4(runtime, hq_corp.host_name, "10.0.10.2"),
        "hq_edge_frr": bool(
            exec_or_empty(
                runtime, edge_name_for("hq"), "pgrep -x bgpd", timeout=10
            ).strip()
        ),
    }
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
        details={"startup_check": True, "topo_size": topo_size},
    )


def verify_enterprise_branch_lab(
    runtime: LabRuntime,
    *,
    scenario_name: str,
    topo_size: TopoSize,
    built_tunnels: list[BuiltTunnel] | None = None,
    spec: TopoSpec | None = None,
    **_kwargs: Any,
) -> dict[str, Any]:
    spec = spec or build_topo_spec(topo_size)
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
        vrf_out = exec_or_empty(runtime, edge, "ip -d link show type vrf", timeout=15)
        return {f"vrf_{site.name}_{lan.role}": lan.vrf in vrf_out for lan in site.lans}

    for site_checks in bounded_parallel_map(site_vrf_checks, spec.sites.values()):
        checks.update(site_checks)

    # --- Underlay + WG + BGP: every designed tunnel ---
    def tunnel_checks(bt: BuiltTunnel) -> dict[str, bool]:
        spoke_edge = edge_name_for(bt.spoke)
        hub_edge = edge_name_for(bt.hub)
        tag = f"{bt.spoke}_{bt.hub}_{bt.provider}_{'pri' if bt.primary else 'bak'}"
        result = {
            f"underlay_{tag}_spoke_to_hub": ping_ok(runtime, spoke_edge, bt.hub_wan_ip),
            f"underlay_{tag}_hub_to_spoke": ping_ok(runtime, hub_edge, bt.spoke_wan_ip),
            f"wg_{tag}_spoke": bool(
                exec_or_empty(
                    runtime, spoke_edge, f"wg show {bt.spoke_iface}", timeout=15
                ).strip()
            ),
            f"wg_{tag}_hub": bool(
                exec_or_empty(
                    runtime, hub_edge, f"wg show {bt.hub_iface}", timeout=15
                ).strip()
            ),
        }

        spoke_sum = exec_or_empty(
            runtime, spoke_edge, "vtysh -c 'show bgp summary'", timeout=20
        )
        hub_sum = exec_or_empty(
            runtime, hub_edge, "vtysh -c 'show bgp summary'", timeout=20
        )
        result[f"bgp_{tag}_spoke"] = _peer_established(spoke_sum, bt.hub_tunnel_ip)
        result[f"bgp_{tag}_hub"] = _peer_established(hub_sum, bt.spoke_tunnel_ip)
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
        rib = exec_or_empty(runtime, edge, "vtysh -c 'show ip bgp'", timeout=20)
        result[f"overlay_hq_corp_on_{spoke}"] = hq_corp_net in rib
        result[f"overlay_hq_server_on_{spoke}"] = hq_srv_net in rib
        for net in local_only_nets:
            result[f"overlay_no_{net}_on_{spoke}"] = net not in rib

        # CORP VRF RIB: HQ CORP + SERVER (shared-services leak); no guest/iot.
        corp_rib = exec_or_empty(
            runtime,
            edge,
            f"vtysh -c 'show ip route vrf {vrf_name('corp')}'",
            timeout=20,
        )
        if not corp_rib.strip():
            corp_rib = exec_or_empty(
                runtime, edge, f"ip route show vrf {vrf_name('corp')}", timeout=15
            )
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
            local_rib = exec_or_empty(
                runtime,
                edge,
                f"ip route show vrf {lan.vrf}",
                timeout=15,
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
        b_ip = _lan_host_ip(_corp_lan(spec, b))
        route_get = exec_or_empty(
            runtime,
            edge_name_for(a),
            f"ip route get {b_ip} vrf {vrf_name('corp')}",
            timeout=15,
        )
        return {
            f"e2e_{a}_to_{b}": ping_ok(runtime, a_corp.host_name, b_ip),
            f"overlay_path_{a}_to_{b}": "dev wg" in route_get or "172.30." in route_get,
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
        tc_out = exec_or_empty(
            runtime, edge_name, f"tc qdisc show dev {iface}", timeout=15
        )
        tc_class = exec_or_empty(
            runtime, edge_name, f"tc class show dev {iface}", timeout=15
        )
        tag = f"{edge_name}_{iface}"
        return {
            f"qos_htb_{tag}": "htb" in tc_out.lower(),
            f"qos_ef_class_{tag}": "1:10" in tc_class,
            f"qos_be_class_{tag}": "1:20" in tc_class,
            f"qos_rate_{tag}": f"{qos.rate_mbit}Mbit" in tc_class
            or (f"{qos.rate_mbit}mbit" in tc_class.lower()),
        }

    for qos_result in bounded_parallel_map(qos_checks, qos_jobs):
        checks.update(qos_result)

    # --- Dual-path: every backup session up; primary preferred per dual spoke ---
    backups = [t for t in tunnels if not t.primary]
    checks["backup_paths_present"] = bool(backups)

    def backup_check(bt: BuiltTunnel) -> tuple[str, bool]:
        summary = exec_or_empty(
            runtime, edge_name_for(bt.spoke), "vtysh -c 'show bgp summary'", timeout=20
        )
        return (
            f"backup_bgp_{bt.spoke}_{bt.hub}_{bt.provider}",
            _peer_established(summary, bt.hub_tunnel_ip),
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
            runtime,
            edge_name_for(spoke),
            f"vtysh -c 'show ip bgp {hq_corp_cidr}'",
            timeout=20,
        )
        return f"primary_path_{spoke}", primaries[0].hub_tunnel_ip in best

    for result in bounded_parallel_map(primary_check, dual_spokes):
        if result is not None:
            checks[result[0]] = result[1]

    details["failed"] = [k for k, v in checks.items() if not v]
    details["check_count"] = len(checks)
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
        details=details,
    )
