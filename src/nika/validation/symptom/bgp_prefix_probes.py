"""Test-path observations for BGP import prefix-length rejection."""

from __future__ import annotations

from nika.net_env.verify import ping_stats
from nika.problems.base import build_verify_result
from nika.problems.support.polling import wait_until
from nika.validation.symptom.bgp_route_probes import _policy_probe_ends


def prefix_length_baseline(problem, params):
    """Record learned business routes, Established peers and endpoint health."""
    ends = _policy_probe_ends(problem, params.host_name)
    if "error" in ends:
        return False, ends
    reading = {}

    def healthy():
        peers, routes = problem._state(params.host_name)
        loss = ping_stats(
            problem.runtime, ends["src"], ends["dst"], count=5
        ).loss_percent
        reading.update(peers=sorted(peers), routes=sorted(routes), loss=loss)
        return bool(peers and routes) and loss == 0.0

    verified = wait_until(healthy, 90, interval_s=3)
    problem._audit_prefix_lengths = {**reading, "ends": ends}
    return verified, {**reading, **ends}


def prefix_length_drops_routes(problem, params):
    """Imports disappear and traffic fails while the same peers stay up."""
    baseline = getattr(problem, "_audit_prefix_lengths", None)
    if not baseline:
        return False, {"error": "no_healthy_baseline"}
    ends = baseline["ends"]
    reading = {}

    def rejected():
        peers, routes = problem._state(params.host_name)
        missing = set(baseline["routes"]) - routes
        loss = ping_stats(
            problem.runtime, ends["src"], ends["dst"], count=5
        ).loss_percent
        reading.update(
            peers=sorted(peers),
            routes=sorted(routes),
            missing=sorted(missing),
            loss=loss,
        )
        return (
            set(baseline["peers"]) <= peers
            and not routes
            and bool(missing)
            and loss == 100.0
        )

    verified = wait_until(rejected, 90, interval_s=3)
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={"baseline": baseline, **reading},
    )
