"""Distinguish missing BGP IPv4 activation from route-policy rejection."""

from __future__ import annotations

from nika.problems.base import build_verify_result
from nika.problems.support.polling import wait_until


def ipv4_family_baseline(problem, params):
    baseline = problem.record_baseline(params)
    problem._audit_ipv4_family = dict(baseline)
    return True, baseline


def ipv4_family_missing(problem, params):
    baseline = getattr(problem, "_audit_ipv4_family", None)
    if not baseline:
        return False, {"error": "no_healthy_ipv4_family_baseline"}
    reading = {}

    def missing():
        status = problem.ipv4_status(params.host_name, baseline["neighbor_ip"])
        prefixes = problem.ipv4_prefixes(params.host_name, baseline["neighbor_ip"])
        reading.update(status)
        reading["prefixes"] = sorted(prefixes)
        return (
            not status["configured"]
            and not status["negotiated"]
            and status["state"] == "Established"
            and status["received_prefix_count"] == 0
            and not prefixes
        )

    ok = wait_until(missing, 120, interval_s=3)
    return ok, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=ok,
        details={
            "host": params.host_name,
            "neighbor_ip": baseline["neighbor_ip"],
            "baseline": baseline,
            **reading,
        },
    )
