"""Scenario default endpoint paths used by inject helpers and tests."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProbePath:
    """Default traffic endpoints for a scenario (inject host pools / probes)."""

    src_host: str
    dst_ip: str | None = None
    http_url: str | None = None
    symptom_url: str | None = None
    control_url: str | None = None
    control_plane_host: str | None = None
    ping_count: int = 20
    gray_ping_count: int = 100
    http_name_url: str | None = None
    peer_host: str | None = None
    old_ip: str | None = None


_SCENARIO_PROBE_PATHS: dict[str, ProbePath] = {
    "simple_bgp": ProbePath(
        src_host="pc1",
        dst_ip="200.1.1.2",
        http_url=None,
        control_plane_host="router1",
        peer_host="pc2",
    ),
    "dc_clos": ProbePath(
        src_host="client_0",
        dst_ip="10.0.1.2",
        http_url="http://web0.pod0/",
        http_name_url="http://web0.pod0/",
        control_plane_host="leaf_router_0_0",
        peer_host="webserver0_pod0",
    ),
    "campus_lan": ProbePath(
        src_host="pc_1_1_1_1",
        dst_ip="10.200.0.3",
        http_url="http://web0.local/",
        http_name_url="http://web0.local/",
        control_plane_host="router_dist_1_1",
        peer_host="pc_2_1_1_1",
    ),
    "enterprise_branch": ProbePath(
        src_host="br1_corp_pc",
        dst_ip="10.0.20.2",
        http_url="http://10.0.20.2/small.bin",
        control_plane_host="br1_edge",
        peer_host="hq_corp_pc",
    ),
    "sdn_l3_clos": ProbePath(
        src_host="client_1_1",
        dst_ip="10.0.2.11",
        http_url="http://10.0.2.11/",
        control_plane_host="onos",
        peer_host="client_2_1",
    ),
    "p4_dc_fabric": ProbePath(
        src_host="client_1_1",
        dst_ip="10.0.2.11",
        http_url="http://10.0.2.11/",
        control_plane_host="leaf_1",
        peer_host="client_2_1",
    ),
    "p4_dc_gateway": ProbePath(
        src_host="client_1",
        dst_ip="20.0.0.1",
        http_url="http://20.0.0.1/",
        control_plane_host="gateway_1",
        peer_host="client_2",
    ),
    "min3clos": ProbePath(
        src_host="client_1_1",
        dst_ip="10.0.2.11",
        http_url="http://10.0.2.11/",
        control_plane_host="leaf_1",
        peer_host="client_2_1",
    ),
    "k8s_lab": ProbePath(
        src_host="client",
        dst_ip="201.1.1.2",
        http_url="http://datacenter.com/word",
        control_plane_host="controller",
        peer_host="as2r1",
    ),
    "llmd_lab": ProbePath(
        src_host="client",
        dst_ip="10.0.0.2",
        http_url="http://10.0.0.2/",
        control_plane_host="controller",
    ),
    "isp": ProbePath(
        src_host="pc_atlam5",
        dst_ip="10.254.0.6",
        control_plane_host="atlam5",
        peer_host="pc_atlang",
    ),
}


def get_probe_path(scenario: str, *, topo_size: str = "s") -> ProbePath | None:
    if scenario.startswith("isp/"):
        scenario = "isp"
    dynamic = _resolve_dynamic_probe_path(scenario, topo_size)
    if dynamic is not None:
        return dynamic
    return _SCENARIO_PROBE_PATHS.get(scenario)


def _resolve_dynamic_probe_path(scenario: str, topo_size: str) -> ProbePath | None:
    try:
        if scenario in {"sdn_l3_clos", "p4_dc_fabric"}:
            if scenario == "sdn_l3_clos":
                from nika.net_env.sdn_l3_clos.topology_model import (
                    build_clos_fabric_model,
                )

                model = build_clos_fabric_model(topo_size)
                control = "onos"
            else:
                from nika.net_env.p4_dc_fabric.topology_model import (
                    build_clos_fabric_model,
                )

                model = build_clos_fabric_model(topo_size)
                control = "leaf_1"
            clients = model.client_endpoints()
            src = clients[0]
            dst = next(w for w in model.web_endpoints() if w.leaf_id != src.leaf_id)
            peer = next((c for c in clients if c.name != src.name), None)
            return ProbePath(
                src_host=src.name,
                dst_ip=dst.ip,
                http_url=f"http://{dst.ip}/",
                control_plane_host=control,
                peer_host=peer.name if peer else None,
            )
        if scenario == "p4_dc_gateway":
            from nika.net_env.p4_dc_gateway.topology_model import (
                build_gateway_fabric_model,
            )

            model = build_gateway_fabric_model(topo_size)
            client = model.clients[0]
            peer = model.clients[1] if len(model.clients) > 1 else None
            return ProbePath(
                src_host=client.name,
                dst_ip=model.vip_ip,
                http_url=model.vip_url,
                control_plane_host="gateway_1",
                peer_host=peer.name if peer else None,
            )
        if scenario == "isp":
            from nika.net_env.isp.igp import IspConfig, compile_isp_plan
            from nika.net_env.isp.inject_targets import isp_default_probe_path

            plan = compile_isp_plan(IspConfig(topology="abilene", igp="ospf"))
            return isp_default_probe_path(plan.inventory)
    except Exception:  # noqa: BLE001
        return None
    return None
