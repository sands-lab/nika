"""Parse ApacheBench (ab) summary output for RPS and latency percentiles."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


_RPS_RE = re.compile(
    r"Requests per second:\s*([0-9.]+)\s*\[#/sec\]",
    re.IGNORECASE,
)
_COMPLETE_RE = re.compile(r"Complete requests:\s*(\d+)", re.IGNORECASE)
_FAILED_RE = re.compile(r"Failed requests:\s*(\d+)", re.IGNORECASE)
_NON2XX_RE = re.compile(r"Non-2xx responses:\s*(\d+)", re.IGNORECASE)
_PERCENTILE_RE = re.compile(r"^\s*(\d+)%\s+(\d+)\s*$", re.MULTILINE)
# ab may report "0" or omit Non-2xx; treat missing as 0.


@dataclass
class AbSummary:
    """Structured fields from a single ``ab`` run."""

    requests_per_sec: float | None = None
    complete_requests: int | None = None
    failed_requests: int = 0
    non_2xx_responses: int = 0
    percentiles_ms: dict[int, float] = field(default_factory=dict)
    raw: str = ""

    @property
    def p50_ms(self) -> float | None:
        return self.percentiles_ms.get(50)

    @property
    def p95_ms(self) -> float | None:
        return self.percentiles_ms.get(95)

    @property
    def p99_ms(self) -> float | None:
        return self.percentiles_ms.get(99)

    @property
    def error_count(self) -> int:
        return int(self.failed_requests) + int(self.non_2xx_responses)

    @property
    def error_rate(self) -> float | None:
        if not self.complete_requests:
            return None
        return self.error_count / float(self.complete_requests)


def parse_ab_output(text: str) -> AbSummary:
    """Extract RPS, failures, and percentile latencies from ``ab`` stdout."""
    summary = AbSummary(raw=text or "")
    if not text:
        return summary

    m = _RPS_RE.search(text)
    if m:
        summary.requests_per_sec = float(m.group(1))

    m = _COMPLETE_RE.search(text)
    if m:
        summary.complete_requests = int(m.group(1))

    m = _FAILED_RE.search(text)
    if m:
        summary.failed_requests = int(m.group(1))

    m = _NON2XX_RE.search(text)
    if m:
        summary.non_2xx_responses = int(m.group(1))

    for match in _PERCENTILE_RE.finditer(text):
        pct = int(match.group(1))
        summary.percentiles_ms[pct] = float(match.group(2))

    return summary


def ab_summary_to_dict(summary: AbSummary) -> dict:
    """Serialize ``AbSummary`` for verify/recover detail payloads."""
    return {
        "requests_per_sec": summary.requests_per_sec,
        "complete_requests": summary.complete_requests,
        "failed_requests": summary.failed_requests,
        "non_2xx_responses": summary.non_2xx_responses,
        "p50_ms": summary.p50_ms,
        "p95_ms": summary.p95_ms,
        "p99_ms": summary.p99_ms,
        "error_count": summary.error_count,
        "error_rate": summary.error_rate,
    }
