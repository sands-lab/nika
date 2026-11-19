"""Origin-preserving route leak observations through normal router tools."""

from nika.net_env.isp.bgp.config import EBGP_BASE_ASN
from nika.net_env.verify import _parse_ping_stats
from nika.problems.routing_control_plane.route_leak import learned_origin_path
from nika.problems.support.oracle import vtysh


def _reading(problem):
    observer = problem.net_env.inventory["bgp"]["rov_observer"]
    raw = problem.runtime.exec(
        observer, vtysh("show bgp ipv4 unicast 198.51.100.0/24 json")
    )
    return observer, raw


def _leaked(raw):
    return learned_origin_path(
        raw, leaker_asn=EBGP_BASE_ASN + 1, origin_asn=EBGP_BASE_ASN, require_valid=True
    )


def _flow(problem):
    source, source_ip, destination = problem.business_flow()
    ping = _parse_ping_stats(
        problem.runtime.exec(
            source, f"ping -n -I {source_ip} -c 4 -i 0.2 -W 2 {destination}"
        ),
        count=4,
    )
    trace = problem.runtime.exec(
        source, f"traceroute -n -s {source_ip} -q 1 -w 1 -m 32 {destination}"
    )
    return ping, trace


def route_leak_baseline(problem, params):
    observer, raw = _reading(problem)
    ping, trace = _flow(problem)
    ok = not _leaked(raw) and ping.loss_percent == 0 and ping.rtt_avg_ms is not None
    problem._audit_route_leak = {"routes": raw, "trace": trace, "reachable": ok}
    return ok, problem._audit_route_leak


def route_leak_detour(problem, params):
    observer, raw = _reading(problem)
    ping, trace = _flow(problem)
    before = getattr(problem, "_audit_route_leak", {})
    ok = bool(
        before.get("reachable")
        and _leaked(raw)
        and ping.loss_percent == 0
        and trace != before["trace"]
    )
    return ok, {
        "before": before,
        "routes": raw,
        "trace": trace,
        "loss_percent": ping.loss_percent,
    }
