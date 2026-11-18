"""Parse ping command output into structured statistics."""

from __future__ import annotations

import re

from nika.service.pingmesh.types import PingStats, PingStatus

PING_STATS_RE = re.compile(
    r"(?P<tx>\d+)\s+packets transmitted,\s+"
    r"(?P<rx>\d+)\s+(?:packets\s+)?received(?:,\s*\+\d+\s+errors)?,\s+"
    r"(?P<loss>\d+(?:\.\d+)?)%\s+packet loss"
    r"(?:,\s*time\s*(?P<time>\d+)ms)?",
    re.MULTILINE,
)

RTT_RE = re.compile(
    r"(?:rtt|round-trip)\s+min/avg/max(?:/(?:mdev|stddev))?\s*=\s*"
    r"([\d\.]+)/([\d\.]+)/([\d\.]+)(?:/([\d\.]+))?\s*ms",
    re.MULTILINE,
)


def _derive_status(tx: int | None, rx: int | None, loss: float | None) -> PingStatus:
    if tx is not None and rx is not None and loss is not None:
        if rx > 0 and loss < 100:
            return "ok"
        if rx == 0 and loss == 100:
            return "down"
        return "unstable"
    return "unknown"


def parse_ping_output(output: str) -> PingStats:
    """Parse ``ping -c N -n -q`` output into tx/rx/loss/rtt/status."""
    lowered = output.lower()
    if "network is unreachable" in lowered or "name or service not known" in lowered:
        return {
            "tx": None,
            "rx": 0,
            "loss_percent": 100.0,
            "time_ms": None,
            "rtt_min_ms": None,
            "rtt_avg_ms": None,
            "rtt_max_ms": None,
            "rtt_mdev_ms": None,
            "status": "down",
        }

    stats_match = PING_STATS_RE.search(output)
    tx = rx = loss = time_ms = None
    rtt_min = rtt_avg = rtt_max = rtt_mdev = None

    if stats_match:
        tx = int(stats_match.group("tx"))
        rx = int(stats_match.group("rx"))
        loss = float(stats_match.group("loss"))
        if stats_match.group("time") is not None:
            time_ms = float(stats_match.group("time"))

    rtt_match = RTT_RE.search(output)
    if rtt_match:
        rtt_min, rtt_avg, rtt_max, rtt_mdev = (
            float(value) if value is not None else None for value in rtt_match.groups()
        )

    status = _derive_status(tx, rx, loss)
    return {
        "tx": tx,
        "rx": rx,
        "loss_percent": loss,
        "time_ms": time_ms,
        "rtt_min_ms": rtt_min,
        "rtt_avg_ms": rtt_avg,
        "rtt_max_ms": rtt_max,
        "rtt_mdev_ms": rtt_mdev,
        "status": status,
    }
