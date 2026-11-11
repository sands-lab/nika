"""VRF import-policy symptoms and healthy controls (test-path only)."""

from __future__ import annotations

from nika.problems.base import build_verify_result
from nika.problems.support.polling import wait_until


def _remote_prefixes(problem, params, routing):
    from nika.problems.routing_control_plane.vrf import _site_lan

    site, _, _ = _site_lan(problem.net_env, params.host_name, params.vrf_name)
    return (set(routing["overlay"]) & set(routing["vrf"])) - {
        lan.prefix for lan in site.lans
    }


def vrf_import_baseline(problem, params):
    def healthy():
        problem._audit_vrf_routing = problem.routing_readings(params)
        problem._audit_vrf_paths = problem.path_readings(params)
        routing = problem._audit_vrf_routing
        return (
            bool(routing["peers"])
            and bool(_remote_prefixes(problem, params, routing))
            and bool(routing["handshakes"])
            and all(
                age is not None and age < 180 for age in routing["handshakes"].values()
            )
            and all(received == 4 for received in problem._audit_vrf_paths.values())
        )

    ok = wait_until(healthy, 120, interval_s=3)
    return ok, {
        "paths_received": problem._audit_vrf_paths,
        **problem._audit_vrf_routing,
    }


def vrf_import_routes_lost(problem, params):
    baseline = getattr(problem, "_audit_vrf_routing", None)
    if not baseline:
        return False, {"error": "no_healthy_vrf_baseline"}
    remote = _remote_prefixes(problem, params, baseline)
    routing, paths = {}, {}

    def withdrawn():
        nonlocal routing, paths
        routing = problem.routing_readings(params)
        paths = problem.path_readings(params)
        return (
            bool(remote)
            and not remote & set(routing["vrf"])
            and set(baseline["overlay"]) <= set(routing["overlay"])
            and set(baseline["peers"]) <= set(routing["peers"])
            and bool(routing["handshakes"])
            and all(
                age is not None and age < 180 for age in routing["handshakes"].values()
            )
            and paths["remote"] == 0
            and all(
                received == 4 for name, received in paths.items() if name != "remote"
            )
        )

    ok = wait_until(withdrawn, 90, interval_s=3)
    return ok, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=ok,
        details={
            "remote_prefixes_before": sorted(remote),
            "paths_received": paths,
            **routing,
        },
    )
