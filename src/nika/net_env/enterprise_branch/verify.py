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
    for site in spec.sites.values():
        for lan in site.lans:
            for host_index, host_name in enumerate(lan.host_names):
                key = f"host_ipv4_{host_name}"
                checks[key] = host_has_ipv4(
                    runtime, host_name, _lan_host_ip(lan, host_index)
                )

    # --- VRF devices present on every Site Edge ---
    for site in spec.sites.values():
        edge = edge_name_for(site.name)
        vrf_out = exec_or_empty(runtime, edge, "ip -d link show type vrf", timeout=15)
        for lan in site.lans:
            checks[f"vrf_{site.name}_{lan.role}"] = lan.vrf in vrf_out

    # --- Underlay + WG + BGP: every designed tunnel ---
    for bt in tunnels:
        spoke_edge = edge_name_for(bt.spoke)
        hub_edge = edge_name_for(bt.hub)
        tag = f"{bt.spoke}_{bt.hub}_{bt.provider}_{'pri' if bt.primary else 'bak'}"

        checks[f"underlay_{tag}_spoke_to_hub"] = ping_ok(
            runtime, spoke_edge, bt.hub_wan_ip
        )
        checks[f"underlay_{tag}_hub_to_spoke"] = ping_ok(
            runtime, hub_edge, bt.spoke_wan_ip
        )
        checks[f"wg_{tag}_spoke"] = bool(
            exec_or_empty(
                runtime, spoke_edge, f"wg show {bt.spoke_iface}", timeout=15
            ).strip()
        )
        checks[f"wg_{tag}_hub"] = bool(
            exec_or_empty(
                runtime, hub_edge, f"wg show {bt.hub_iface}", timeout=15
            ).strip()
        )

        spoke_sum = exec_or_empty(
            runtime, spoke_edge, "vtysh -c 'show bgp summary'", timeout=20
        )
        hub_sum = exec_or_empty(
            runtime, hub_edge, "vtysh -c 'show bgp summary'", timeout=20
        )
        checks[f"bgp_{tag}_spoke"] = _peer_established(spoke_sum, bt.hub_tunnel_ip)
        checks[f"bgp_{tag}_hub"] = _peer_established(hub_sum, bt.spoke_tunnel_ip)

    if not tunnels:
        checks["underlay_any_tunnel"] = False

    # --- Overlay RIB on every spoke: HQ CORP/SERVER present; local-only absent ---
    hq_corp = _corp_lan(spec, "hq")
    hq_srv = _server_lan(spec, "hq")
    hq_corp_net = _prefix_net(hq_corp.prefix)
    hq_srv_net = _prefix_net(hq_srv.prefix)
    local_only_nets = [_prefix_net(p) for p in spec.local_only_prefixes()]
    spokes = spec.branch_names()

    for spoke in spokes:
        edge = edge_name_for(spoke)
        rib = exec_or_empty(runtime, edge, "vtysh -c 'show ip bgp'", timeout=20)
        checks[f"overlay_hq_corp_on_{spoke}"] = hq_corp_net in rib
        checks[f"overlay_hq_server_on_{spoke}"] = hq_srv_net in rib
        for net in local_only_nets:
            checks[f"overlay_no_{net}_on_{spoke}"] = net not in rib

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
        checks[f"vrf_corp_hq_corp_on_{spoke}"] = _rib_has_prefix(
            corp_rib, hq_corp.prefix
        )
        checks[f"vrf_corp_hq_server_on_{spoke}"] = _rib_has_prefix(
            corp_rib, hq_srv.prefix
        )
        for net in local_only_nets:
            checks[f"vrf_corp_no_{net}_on_{spoke}"] = net not in corp_rib

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
            checks[f"vrf_{lan.role}_no_hq_corp_on_{spoke}"] = (
                hq_corp_net not in local_rib
            )
            checks[f"vrf_{lan.role}_no_hq_server_on_{spoke}"] = (
                hq_srv_net not in local_rib
            )

    # --- E2E: every branch CORP <-> HQ CORP and HTTP to HQ SERVER ---
    hq_corp_ip = _lan_host_ip(hq_corp)
    hq_srv_ip = _lan_host_ip(hq_srv)
    for spoke in spokes:
        corp = _corp_lan(spec, spoke)
        corp_ip = _lan_host_ip(corp)
        checks[f"e2e_{spoke}_to_hq_corp"] = ping_ok(runtime, corp.host_name, hq_corp_ip)
        checks[f"e2e_hq_to_{spoke}_corp"] = ping_ok(runtime, hq_corp.host_name, corp_ip)
        checks[f"e2e_{spoke}_http_hq_server"] = http_ok(
            runtime, corp.host_name, f"http://{hq_srv_ip}/"
        )

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
    for i, a in enumerate(spokes):
        for b in spokes[i + 1 :]:
            a_corp = _corp_lan(spec, a)
            b_corp = _corp_lan(spec, b)
            b_ip = _lan_host_ip(b_corp)
            checks[f"e2e_{a}_to_{b}"] = ping_ok(runtime, a_corp.host_name, b_ip)
            route_get = exec_or_empty(
                runtime,
                edge_name_for(a),
                f"ip route get {b_ip} vrf {vrf_name('corp')}",
                timeout=15,
            )
            via_wg = "dev wg" in route_get or "172.30." in route_get
            checks[f"overlay_path_{a}_to_{b}"] = via_wg

    # --- Providers: no enterprise prefixes on any ISP ---
    for provider in spec.providers:
        isp = isp_name_for(provider)
        isp_routes = exec_or_empty(runtime, isp, "ip route", timeout=15)
        isp_frr = exec_or_empty(runtime, isp, "vtysh -c 'show ip route'", timeout=15)
        leaked = any(
            p.split("/")[0] in isp_routes or p.split("/")[0] in isp_frr
            for p in spec.advertised_prefixes()
        )
        checks[f"provider_{provider}_no_enterprise"] = not leaked

    # --- Cross-VRF isolation: guest/iot vs remote CORP; guest vs local CORP ---
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
                checks[f"{lan.role}_{host_name}_isolated"] = not ping_ok(
                    runtime, host_name, remote_ip, count=1
                )
            # Same-site CORP must also be unreachable from local-only VRFs.
            local_corp = _corp_lan(spec, site.name)
            local_corp_ip = _lan_host_ip(local_corp)
            for host_name in lan.host_names:
                checks[f"{lan.role}_{host_name}_no_local_corp"] = not ping_ok(
                    runtime, host_name, local_corp_ip, count=1
                )

    # --- Overlay egress QoS: HTB EF/BE classes on every WireGuard iface ---
    qos = overlay_qos_for(topo_size)
    seen_qos: set[tuple[str, str]] = set()
    for bt in tunnels:
        for edge_name, iface in (
            (edge_name_for(bt.spoke), bt.spoke_iface),
            (edge_name_for(bt.hub), bt.hub_iface),
        ):
            key = (edge_name, iface)
            if key in seen_qos:
                continue
            seen_qos.add(key)
            tc_out = exec_or_empty(
                runtime, edge_name, f"tc qdisc show dev {iface}", timeout=15
            )
            tc_class = exec_or_empty(
                runtime, edge_name, f"tc class show dev {iface}", timeout=15
            )
            tag = f"{edge_name}_{iface}"
            checks[f"qos_htb_{tag}"] = "htb" in tc_out.lower()
            checks[f"qos_ef_class_{tag}"] = "1:10" in tc_class
            checks[f"qos_be_class_{tag}"] = "1:20" in tc_class
            checks[f"qos_rate_{tag}"] = f"{qos.rate_mbit}Mbit" in tc_class or (
                f"{qos.rate_mbit}mbit" in tc_class.lower()
            )

    # --- Dual-path: every backup session up; primary preferred per dual spoke ---
    backups = [t for t in tunnels if not t.primary]
    checks["backup_paths_present"] = bool(backups)
    for bt in backups:
        summary = exec_or_empty(
            runtime, edge_name_for(bt.spoke), "vtysh -c 'show bgp summary'", timeout=20
        )
        checks[f"backup_bgp_{bt.spoke}_{bt.hub}_{bt.provider}"] = _peer_established(
            summary, bt.hub_tunnel_ip
        )

    dual_spokes = sorted({t.spoke for t in tunnels if not t.primary})
    hq_corp_cidr = hq_corp.prefix
    for spoke in dual_spokes:
        if spoke not in spokes:
            # Hub interconnect backups are checked above; primary-pref is spoke-only.
            continue
        primaries = [t for t in tunnels if t.spoke == spoke and t.primary]
        if not primaries:
            continue
        best = exec_or_empty(
            runtime,
            edge_name_for(spoke),
            f"vtysh -c 'show ip bgp {hq_corp_cidr}'",
            timeout=20,
        )
        checks[f"primary_path_{spoke}"] = primaries[0].hub_tunnel_ip in best

    details["failed"] = [k for k, v in checks.items() if not v]
    details["check_count"] = len(checks)
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
        details=details,
    )
