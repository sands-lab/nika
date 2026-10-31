"""P4Runtime, BMv2, and INT telemetry steps for oracle playbooks of P4 failures."""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable

from nika.problems.support.oracle import Check, OracleStep, p4rt, shell


def p4rt_json(description: str, predicate: Callable[[dict], bool]) -> Check:
    """The ``p4rt_exec`` output is complete JSON that satisfies *predicate*."""

    def check(output: str) -> bool:
        start = output.find("{")
        try:
            data = json.loads(output[start:]) if start >= 0 else None
            return isinstance(data, dict) and bool(predicate(data))
        except (ValueError, KeyError, TypeError, AttributeError):
            return False

    return Check(description, check)


def table_read(
    switch: str,
    table: str,
    description: str,
    predicate: Callable[[list[dict]], bool],
    purpose: str,
    *,
    evidence: bool = True,
) -> OracleStep:
    """Evidence from every live entry of one P4 table on *switch*."""
    return p4rt(
        f"read-table --switch {switch} --table {table}",
        purpose,
        p4rt_json(description, lambda data: predicate(data["tables"][table])),
        evidence=evidence,
    )


def bmv2_ports(switch: str, expect: Check | None = None) -> OracleStep:
    """BMv2 port-to-interface bindings from the running switch command line."""
    return shell(
        switch,
        "pgrep -af '[s]imple_switch' | grep -oE '[0-9]+@[A-Za-z0-9_.-]+' "
        "| tr '\\n' ' '",
        f"Map BMv2 ports to interfaces on {switch}.",
        expect,
    )


def summary_regex(switch: str, body: str) -> str:
    """Regex over *switch*'s entry in the ``read`` summary (no nested objects)."""
    return rf'"{switch}":\s*\{{[^{{}}]*?{body}'


# INT-MX trace outcomes per (switch, egress port, destination), computed on the
# telemetry collector: traces that reached the sink count as delivered at every
# hop they crossed; traces that never did count as lost at their last hop.
_INT_PATH_SUMMARY = """
import collections, json, sys, time
window, switch = float(sys.argv[1]), int(sys.argv[2])
now = time.time_ns()
latest = {}
with open("/var/lib/int-collector/int_reports.jsonl") as handle:
    for line in collections.deque(handle, maxlen=30000):
        try:
            row = json.loads(line)
            latest[(row["flow_id"], row["packet_id"])] = row
        except (ValueError, KeyError):
            pass
stats = collections.defaultdict(lambda: [0, 0])
for row in latest.values():
    ts = int(row["packet_timestamp"])
    hops = sorted(row["hop_sequence"], key=lambda hop: hop["egress_timestamp"])
    if ts < now - window * 1e9 or not hops:
        continue
    if row["sink_seen"]:
        for hop in hops:
            stats[(hop["switch_id"], hop["egress_port"], row["dst"])][0] += 1
    elif now - ts > 2e9:
        hop = hops[-1]
        stats[(hop["switch_id"], hop["egress_port"], row["dst"])][1] += 1
for (sid, port, dst), (ok, lost) in sorted(stats.items()):
    if sid == switch or lost:
        print(f"switch_id={sid} egress_port={port} dst={dst} delivered={ok} lost={lost}")
"""

_INT_ROW = re.compile(
    r"switch_id=(\d+) egress_port=(\d+) dst=(\S+) delivered=(\d+) lost=(\d+)"
)


def int_path_rows(output: str) -> list[tuple[int, int, str, int, int]]:
    """Rows of the INT path summary: (switch_id, port, dst, delivered, lost)."""
    return [
        (int(s), int(p), d, int(ok), int(lost))
        for s, p, d, ok, lost in _INT_ROW.findall(output)
    ]


def int_path_summary(
    switch_id: int,
    window_sec: int,
    expect: Check,
    purpose: str,
    *,
    attempts: int = 1,
    interval_sec: float = 5.0,
) -> OracleStep:
    """Delivered/lost INT-MX traces per egress port, read on the collector."""
    return shell(
        "collector",
        f"python3 -c {shlex.quote(_INT_PATH_SUMMARY)} {window_sec} {switch_id} "
        "| head -80",
        purpose,
        expect,
        evidence=True,
        timeout=30,
        attempts=attempts,
        interval_sec=interval_sec,
    )
