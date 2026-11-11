"""Observe HTTPS rejection of one server while plain HTTP and its CA still work."""

from __future__ import annotations

from typing import Any

from nika.problems.base import build_verify_result


def tls_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The server validates over HTTPS and answers plain HTTP before the fault."""
    sample = problem.client_sample(params)
    problem._audit_tls_baseline = sample
    healthy = (
        sample["https"]["validates"]
        and sample["http_ok"]
        and sample["control"]["validates"]
    )
    return healthy, sample


def tls_expired(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """HTTPS clients reject the expired certificate; HTTP and the control do not fail."""
    base = getattr(problem, "_audit_tls_baseline", None)
    if not base:
        return False, {"error": "no_healthy_baseline"}
    after = problem.client_sample(params)
    verified = bool(
        after["https"]["expired"] and after["http_ok"] and after["control"]["validates"]
    )
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={"baseline": base, "after": after},
    )
