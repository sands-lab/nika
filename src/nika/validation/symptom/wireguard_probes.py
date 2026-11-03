"""WireGuard peer reachability, including a latent standby peer failure."""

from nika.net_env.enterprise_branch.edge_view import FrrEdgeView
from nika.net_env.enterprise_branch.routeros import RouterOsEdgeView
from nika.validation.symptom.path_probes import echo


def _reading(problem, params):
    view = (
        RouterOsEdgeView()
        if params.host_name in problem.net_env.native_routers
        else FrrEdgeView()
    )
    established = view.bgp_peers.established_peers(
        problem.runtime.exec(params.host_name, view.bgp_peers.command)
    )
    tunnels = problem._target_tunnels(params)
    site = problem.net_env.spec.sites[tunnels[0].spoke]
    source = next(lan.host_names[0] for lan in site.lans if lan.role == "corp")
    path = problem.net_env.default_probe_path()
    return {
        "established": sorted(established),
        "target_peers": sorted(t.hub_tunnel_ip for t in tunnels),
        "path": echo(problem.runtime, source, path.dst_ip, count=5),
    }


def peer_key_baseline(problem, params):
    reading = _reading(problem, params)
    healthy = (
        bool(reading["target_peers"])
        and set(reading["target_peers"]) <= set(reading["established"])
        and reading["path"]["received"] == 5
    )
    problem._audit_wireguard = reading if healthy else None
    return healthy, reading


def peer_key_unreachable(problem, params):
    if not getattr(problem, "_audit_wireguard", None):
        return False, {"error": "no_healthy_wireguard_baseline"}
    reading = _reading(problem, params)
    peer_lost = not set(reading["target_peers"]) & set(reading["established"])
    # A single standby peer can fail while the primary carries the user path.
    expected_path = params.scope == "interface" or reading["path"]["received"] == 0
    return bool(reading["target_peers"]) and peer_lost and expected_path, reading
