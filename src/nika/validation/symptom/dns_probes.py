"""DNS server symptom probes (test-path only).

A client fetches the scenario's web page by name, as a user would. The control
fetches the same page from the same client with the name pinned to the
server's address (``curl --resolve``), so it needs no DNS but crosses the same
path to the same web server. A DNS root cause breaks or slows only the first.

Every predicate is also evaluated on the healthy lab: the baseline handler
records the healthy reading that the symptom handler compares against.
"""

from __future__ import annotations

import re
import shlex
from typing import Any
from urllib.parse import urlsplit

from nika.net_env.net_env_pool import get_probe_path
from nika.net_env.verify import median_float
from nika.problems.base import build_verify_result

_SAMPLES = 3
_MAX_TIME_SEC = 25
# curl exit codes: 6 = could not resolve host, 28 = timed out.
_RESOLVE_FAILED = {6, 28}
# The injected lookup delay must be visible, and absent from the control.
_LOOKUP_MIN_MS = 500.0
_LOOKUP_FACTOR = 2.0


def _fetch(
    runtime: Any, host: str, url: str, pin_ip: str | None = None
) -> dict[str, Any]:
    """One HTTP GET; ``pin_ip`` answers the name without DNS."""
    resolve = ""
    if pin_ip:
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        resolve = f"--resolve {shlex.quote(f'{parts.hostname}:{port}:{pin_ip}')} "
    out = runtime.exec(
        host,
        "curl -s -o /dev/null "
        "-w '%{http_code} %{time_namelookup} %{time_connect} %{time_total}' "
        f"--connect-timeout 10 --max-time {_MAX_TIME_SEC} {resolve}"
        f"{shlex.quote(url)}; printf ' %s' $?",
        timeout=_MAX_TIME_SEC + 10,
    )
    match = re.search(r"(\d{3}) ([\d.]+) ([\d.]+) ([\d.]+) (\d+)\s*$", out.strip())
    if match is None:
        return {"ok": False, "parsed": False, "raw": out[-200:]}
    code, lookup, connect, total, rc = match.groups()
    return {
        "ok": code in {"200", "206"},
        "parsed": True,
        "http_code": code,
        "exit_code": int(rc),
        "lookup_ms": round(float(lookup) * 1000.0, 1),
        "connected": float(connect) > 0.0,
        "total_ms": round(float(total) * 1000.0, 1),
    }


def _sweep(
    runtime: Any, host: str, url: str, pin_ip: str | None = None
) -> dict[str, Any]:
    samples = [_fetch(runtime, host, url, pin_ip) for _ in range(_SAMPLES)]
    parsed = [s for s in samples if s["parsed"]]
    return {
        "url": url,
        "pinned_ip": pin_ip,
        "samples": samples,
        "parsed": len(parsed) == len(samples),
        "ok": sum(s["ok"] for s in parsed),
        "resolve_failed": sum(
            not s["connected"] and s["exit_code"] in _RESOLVE_FAILED for s in parsed
        ),
        "median_lookup_ms": median_float([s["lookup_ms"] for s in parsed if s["ok"]]),
        "median_total_ms": median_float([s["total_ms"] for s in parsed if s["ok"]]),
    }


def _ends(problem: Any, params: Any) -> dict[str, Any] | None:
    """Client, name URL and web server address from the scenario probe path."""
    path = get_probe_path(problem.scenario_name or "")
    if path is None or not path.http_name_url or not path.dst_ip:
        return None
    website = getattr(params, "target_website", None)
    domain = getattr(params, "target_domain", None)
    if website and domain:
        # A record fault targets one name; its healthy address is read before inject.
        return {"client": path.src_host, "url": f"http://{website}.{domain}/"}
    return {"client": path.src_host, "url": path.http_name_url, "ip": path.dst_ip}


def _measure(problem: Any, params: Any, ip: str | None = None) -> dict[str, Any]:
    ends = _ends(problem, params)
    if ends is None:
        return {"error": "no_name_probe_path"}
    ip = ip or ends.get("ip")
    runtime = problem.runtime
    return {
        "client": ends["client"],
        "by_name": _sweep(runtime, ends["client"], ends["url"]),
        "by_address": _sweep(runtime, ends["client"], ends["url"], ip) if ip else None,
    }


def _answers(runtime: Any, host: str, url: str) -> list[str]:
    name = urlsplit(url).hostname or ""
    out = runtime.exec(host, f"dig +short +time=3 +tries=2 A {shlex.quote(name)}")
    return sorted(
        {line for line in out.split() if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", line)}
    )


def dns_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Healthy reading: the page loads by name and with the name pinned."""
    ip = None
    ends = _ends(problem, params)
    answers: list[str] | None = None
    if ends is not None and "ip" not in ends:
        answers = _answers(problem.runtime, ends["client"], ends["url"])
        ip = answers[0] if len(answers) == 1 else None
    sample = _measure(problem, params, ip)
    if "error" in sample:
        return False, sample
    sample["answers"] = answers
    problem._audit_dns_baseline = sample
    healthy = bool(
        sample["by_name"]["ok"] == _SAMPLES
        and sample["by_address"]
        and sample["by_address"]["ok"] == _SAMPLES
    )
    return healthy, sample


def _observe(problem: Any, params: Any) -> tuple[dict | None, dict[str, Any]]:
    base = getattr(problem, "_audit_dns_baseline", None)
    ip = (base or {}).get("by_address", {}) or {}
    sample = _measure(problem, params, ip.get("pinned_ip"))
    if "error" in sample:
        return None, sample
    if not sample["by_name"]["parsed"] or not (sample["by_address"] or {}).get(
        "parsed"
    ):
        return None, {**sample, "error": "curl_output_unparsed"}
    control_ok = sample["by_address"]["ok"] == _SAMPLES
    return base, {**sample, "control_ok": control_ok}


def dns_name_unresolvable(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Every fetch by name fails to resolve; the pinned fetch still loads."""
    _, details = _observe(problem, params)
    if "error" in details:
        return False, details
    verified = bool(
        details["by_name"]["resolve_failed"] == _SAMPLES and details["control_ok"]
    )
    return verified, build_verify_result(
        fault_type=problem.root_cause_name, verified=verified, details=details
    )


def dns_lookup_slow(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Name lookups take far longer than before; the pinned fetch stays fast."""
    base, details = _observe(problem, params)
    if "error" in details:
        return False, details
    before = ((base or {}).get("by_name") or {}).get("median_lookup_ms")
    after = details["by_name"]["median_lookup_ms"]
    pinned = details["by_address"]["median_total_ms"]
    slow = bool(
        before is not None
        and after is not None
        and details["by_name"]["ok"] == _SAMPLES
        and after >= _LOOKUP_MIN_MS
        and after >= before * _LOOKUP_FACTOR
    )
    # The page itself may be slow (campus web0 answers in about a second), so
    # the pinned fetch is judged against its own healthy time.
    pinned_before = ((base or {}).get("by_address") or {}).get("median_total_ms")
    control_ok = bool(
        details["control_ok"]
        and pinned is not None
        and pinned_before is not None
        and pinned < pinned_before + _LOOKUP_MIN_MS
    )
    verified = slow and control_ok
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            **details,
            "baseline_median_lookup_ms": before,
            "baseline_pinned_total_ms": pinned_before,
            "lookup_slow": slow,
            "control_ok": control_ok,
        },
    )


def dns_wrong_record(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The client resolves the name to another address and the page fails."""
    base, details = _observe(problem, params)
    if "error" in details:
        return False, details
    ends = _ends(problem, params) or {}
    answers = _answers(problem.runtime, details["client"], ends.get("url", ""))
    before = (base or {}).get("answers")
    changed = bool(before and answers and answers != before)
    verified = bool(changed and details["by_name"]["ok"] == 0 and details["control_ok"])
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            **details,
            "baseline_answers": before,
            "answers": answers,
            "answer_changed": changed,
        },
    )
