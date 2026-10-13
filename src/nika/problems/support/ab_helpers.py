"""ApacheBench (ab) load workers and summary parsing shared by HTTP failures."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nika.runtime.base import LabRuntime

# Agents can list processes and files on lab nodes, so worker argv and log
# paths use neutral load-test names that do not identify the injected fault.
AB_WORKER_TAG = "ab-loop-"
_AB_WORKER_LOG = "/var/tmp/http-load-{worker}.log"
_AB_INSTALL_LOG = "/var/tmp/apt-apache2-utils.log"


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


def _worker_pattern() -> str:
    # Bracket the first character so pgrep/pkill never match their own shell.
    return f"[{AB_WORKER_TAG[0]}]{AB_WORKER_TAG[1:]}"


def ensure_ab(runtime: "LabRuntime", host: str) -> None:
    """Install apache2-utils on ``host`` when ``ab`` is missing."""
    check = runtime.exec(
        host, "command -v ab >/dev/null 2>&1 && echo OK || echo MISSING", timeout=10
    ).strip()
    if "OK" in check:
        return
    install = runtime.exec(
        host,
        "export DEBIAN_FRONTEND=noninteractive; "
        "apt-get update -qq && "
        f"apt-get install -y -qq apache2-utils >{_AB_INSTALL_LOG} 2>&1; "
        "command -v ab >/dev/null 2>&1 && echo OK || echo FAIL",
        timeout=180,
    ).strip()
    if "OK" not in install:
        log = runtime.exec(
            host, f"tail -n 40 {_AB_INSTALL_LOG} 2>/dev/null || true", timeout=10
        )
        raise RuntimeError(f"apachebench (ab) unavailable on {host}: {log!r}")


def start_ab_workers(
    runtime: "LabRuntime",
    host: str,
    url: str,
    *,
    workers: int,
    concurrency: int,
    timeout: float = 20,
) -> None:
    """Replace any running workers on ``host`` with ``workers`` looping ``ab`` runs."""
    stop_ab_workers(runtime, host)
    inner = (
        "while true; do "
        f"ab -n 200000000 -c {int(concurrency)} {shlex.quote(url)}; "
        "sleep 0.05; done"
    )
    cmds = [
        "nohup bash -c "
        + shlex.quote(inner)
        + f" {AB_WORKER_TAG}{worker} </dev/null "
        + f">{_AB_WORKER_LOG.format(worker=worker)} 2>&1 &"
        for worker in range(int(workers))
    ]
    runtime.exec(
        host,
        "command -v ab >/dev/null 2>&1 || exit 127; " + " ".join(cmds),
        timeout=timeout,
    )


def _last_int(output: str) -> int:
    try:
        return int(output.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return 0


def count_ab_workers(runtime: "LabRuntime", host: str) -> tuple[int, int, str]:
    """Return (worker loops, running ab processes, raw state) on ``host``."""
    worker_output = runtime.exec(
        host,
        f"ps -eo args 2>/dev/null | grep -c '{_worker_pattern()}' || true",
        timeout=10,
    ).strip()
    ab_output = runtime.exec(
        host, "ps -eo comm 2>/dev/null | grep -c '^ab$' || true", timeout=10
    ).strip()
    state = f"workers={worker_output!r} ab={ab_output!r}"
    return _last_int(worker_output), _last_int(ab_output), state


def stop_ab_workers(runtime: "LabRuntime", host: str) -> None:
    """Stop worker loops and any ``ab`` process on ``host``."""
    runtime.exec(
        host,
        f"pkill -f '{_worker_pattern()}' 2>/dev/null || true; "
        "pkill -x ab 2>/dev/null || true",
        timeout=15,
    )


def ab_worker_log_tail(runtime: "LabRuntime", host: str, lines: int = 30) -> str:
    """Return the tail of the first worker's log for readiness diagnostics."""
    return runtime.exec(
        host,
        f"tail -n {int(lines)} {_AB_WORKER_LOG.format(worker=0)} 2>/dev/null || true",
        timeout=10,
    )
