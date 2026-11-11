"""Observe HTTPS rejection of one server while plain HTTP and its CA still work."""

from __future__ import annotations

from typing import Any

from nika.problems.base import build_verify_result


def _serves_everything_but_tls(sample: dict[str, Any]) -> bool:
    return bool(
        sample["http_ok"] and sample["backend_ok"] and sample["control"]["validates"]
    )


def tls_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The server validates over HTTPS and serves HTTP and its backend before the fault."""
    sample = problem.symptom_sample(params)
    problem._audit_tls_baseline = sample
    healthy = sample["https"]["validates"] and _serves_everything_but_tls(sample)
    return healthy, sample


def tls_expired(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """HTTPS clients reject the expired certificate; HTTP, the backend, and the control do not fail."""
    base = getattr(problem, "_audit_tls_baseline", None)
    if not base:
        return False, {"error": "no_healthy_baseline"}
    after = problem.symptom_sample(params)
    verified = bool(after["https"]["expired"] and _serves_everything_but_tls(after))
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={"baseline": base, "after": after},
    )
