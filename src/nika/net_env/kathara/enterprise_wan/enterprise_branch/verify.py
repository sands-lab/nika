"""Healthy-state verification for enterprise_branch."""

from __future__ import annotations

from typing import Any

from nika.net_env.kathara.enterprise_wan.enterprise_branch.addressing import (
    edge_name_for,
    isp_name_for,
)
from nika.net_env.kathara.enterprise_wan.enterprise_branch.topology import (
    BuiltTunnel,
    LanSpec,
    TopoSize,
    TopoSpec,
    build_topo_spec,
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


def _lan_host_ip(lan: LanSpec) -> str:
    return lan.prefix.replace(".0/24", ".2")


def _corp_lan(spec: TopoSpec, site: str) -> LanSpec:
    return next(lan for lan in spec.sites[site].lans if lan.role == "corp")


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
            key = f"host_ipv4_{lan.host_name}"
            checks[key] = host_has_ipv4(runtime, lan.host_name, _lan_host_ip(lan))

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

    # --- Overlay RIB on every spoke: HQ CORP/SERVER present; guests absent ---
    hq_corp = _corp_lan(spec, "hq")
    hq_srv = next(lan for lan in spec.sites["hq"].lans if lan.role == "server")
    guest_nets = [g.split("/")[0] for g in spec.guest_prefixes()]
    spokes = [name for name, site in spec.sites.items() if not site.is_hub]

    for spoke in spokes:
        rib = exec_or_empty(
            runtime, edge_name_for(spoke), "vtysh -c 'show ip bgp'", timeout=20
        )
        checks[f"overlay_hq_corp_on_{spoke}"] = "10.0.10.0" in rib
        checks[f"overlay_hq_server_on_{spoke}"] = "10.0.20.0" in rib
        for guest_net in guest_nets:
            checks[f"overlay_no_{guest_net}_on_{spoke}"] = guest_net not in rib

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

    # --- Large: HQ CORP <-> DC2 CORP when secondary hub exists ---
    if "dc2" in spec.sites:
        dc2_corp = _corp_lan(spec, "dc2")
        checks["e2e_hq_to_dc2_corp"] = ping_ok(
            runtime, hq_corp.host_name, _lan_host_ip(dc2_corp)
        )
        checks["e2e_dc2_to_hq_corp"] = ping_ok(runtime, dc2_corp.host_name, hq_corp_ip)
        checks["e2e_dc2_http_hq_server"] = http_ok(
            runtime, dc2_corp.host_name, f"http://{hq_srv_ip}/"
        )

    # --- Spoke-spoke: every pair, via overlay (not provider underlay) ---
    for i, a in enumerate(spokes):
        for b in spokes[i + 1 :]:
            a_corp = _corp_lan(spec, a)
            b_corp = _corp_lan(spec, b)
            b_ip = _lan_host_ip(b_corp)
            checks[f"e2e_{a}_to_{b}"] = ping_ok(runtime, a_corp.host_name, b_ip)
            route_get = exec_or_empty(
                runtime, edge_name_for(a), f"ip route get {b_ip}", timeout=15
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

    # --- Guest isolation: every guest vs a remote CORP ---
    for site in spec.sites.values():
        for lan in site.lans:
            if lan.role != "guest":
                continue
            if site.name == "hq":
                remote_corp = _corp_lan(spec, spokes[0]) if spokes else None
            else:
                remote_corp = hq_corp
            if remote_corp is None:
                checks[f"guest_{lan.host_name}_isolated"] = True
                continue
            checks[f"guest_{lan.host_name}_isolated"] = not ping_ok(
                runtime, lan.host_name, _lan_host_ip(remote_corp), count=1
            )

    # --- Dual-path: every backup session up; primary preferred per dual spoke ---
    backups = [t for t in tunnels if not t.primary]
    if not backups:
        checks["backup_paths_present"] = True
    for bt in backups:
        summary = exec_or_empty(
            runtime, edge_name_for(bt.spoke), "vtysh -c 'show bgp summary'", timeout=20
        )
        checks[f"backup_bgp_{bt.spoke}_{bt.hub}_{bt.provider}"] = _peer_established(
            summary, bt.hub_tunnel_ip
        )

    dual_spokes = sorted({t.spoke for t in tunnels if not t.primary})
    for spoke in dual_spokes:
        primaries = [t for t in tunnels if t.spoke == spoke and t.primary]
        if not primaries:
            continue
        best = exec_or_empty(
            runtime,
            edge_name_for(spoke),
            "vtysh -c 'show ip bgp 10.0.10.0/24'",
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
