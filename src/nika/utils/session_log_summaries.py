"""Human-readable summaries for operator session logs (nika.jsonl)."""

from __future__ import annotations

from typing import Any

_SUMMARY_CAP = 320
_DETAIL_ITEM_CAP = 80


def _truncate(text: str, limit: int = _SUMMARY_CAP) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _compact_value(value: Any, *, limit: int = _DETAIL_ITEM_CAP) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _truncate(value, limit)
    if isinstance(value, dict):
        parts = []
        for key, item in list(value.items())[:6]:
            parts.append(f"{key}={_compact_value(item, limit=40)}")
        extra = len(value) - len(parts)
        body = ", ".join(parts)
        if extra > 0:
            body = f"{body}, +{extra} more" if body else f"+{extra} more"
        return _truncate(f"{{{body}}}", limit)
    if isinstance(value, (list, tuple)):
        parts = [_compact_value(item, limit=40) for item in list(value)[:6]]
        extra = len(value) - len(parts)
        body = ", ".join(parts)
        if extra > 0:
            body = f"{body}, +{extra} more" if body else f"+{extra} more"
        return _truncate(f"[{body}]", limit)
    return _truncate(str(value), limit)


def failed_checks_map(checks: dict[str, Any] | None) -> dict[str, bool]:
    """Return only checks that are not truthy."""
    return {name: bool(ok) for name, ok in (checks or {}).items() if not ok}


def summarize_lab_verify(result: dict[str, Any] | None) -> str:
    """One-line narrative of lab readiness checks."""
    if not result:
        return "no verification result"
    checks = result.get("checks") or {}
    passed = [name for name, ok in checks.items() if ok]
    failed = [name for name, ok in checks.items() if not ok]
    parts: list[str] = []
    if result.get("verified"):
        parts.append("passed")
    else:
        parts.append("failed")
    if passed:
        parts.append(f"ok=[{', '.join(passed)}]")
    if failed:
        parts.append(f"failed=[{', '.join(failed)}]")
    details = result.get("details") or {}
    if details:
        # Prefer probe inventory over large nested validation dumps.
        interesting = {
            key: value
            for key, value in details.items()
            if key != "validation" and value not in (None, "", {}, [])
        }
        if interesting:
            parts.append(f"details={_compact_value(interesting)}")
    scenario = result.get("scenario_name")
    if scenario:
        parts.insert(0, f"scenario={scenario}")
    return _truncate("; ".join(parts))


def summarize_injection(
    problem_names: list[str] | str,
    params_snapshot: dict[str, Any] | None = None,
    resolved_params: dict[str, Any] | None = None,
) -> str:
    """One-line narrative of what injection intended / applied."""
    if isinstance(problem_names, str):
        names = [problem_names]
    else:
        names = list(problem_names)
    snapshot = dict(params_snapshot or {})
    resolved = resolved_params
    if resolved is None:
        raw = snapshot.get("resolved_params")
        resolved = raw if isinstance(raw, dict) else {}
    else:
        resolved = dict(resolved)

    keys = (
        "host_name",
        "host",
        "intf_name",
        "intf",
        "faulty_intf",
        "delay_ms",
        "jitter_ms",
        "loss",
        "corrupt",
        "rate",
        "burst",
        "limit",
        "target_host",
        "target_website",
        "target_domain",
        "service_name",
        "attacker_device",
        "neighbor",
        "asn",
        "prefix",
        "k8s_namespace",
        "k8s_workload",
    )
    fields: list[str] = []
    seen: set[str] = set()
    for source in (resolved, snapshot):
        for key in keys:
            if key in seen or key not in source:
                continue
            value = source[key]
            if value in (None, "", [], {}):
                continue
            seen.add(key)
            fields.append(f"{key}={_compact_value(value, limit=60)}")
    name_text = ",".join(names) if names else "unknown"
    if fields:
        return _truncate(f"{name_text}: {', '.join(fields)}")
    problem_class = snapshot.get("problem_class")
    if problem_class:
        return _truncate(f"{name_text} ({problem_class})")
    return _truncate(name_text)


def summarize_fault_verify(verify_result: dict[str, Any] | None) -> str:
    """One-line narrative of fault verification evidence."""
    if not verify_result:
        return "no verify result"
    parts: list[str] = []
    fault_type = verify_result.get("fault_type")
    if fault_type:
        parts.append(str(fault_type))
    parts.append("verified" if verify_result.get("verified") else "not verified")
    details = verify_result.get("details") or {}
    if details:
        parts.append(f"details={_compact_value(details)}")
    return _truncate("; ".join(parts))
