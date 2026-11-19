"""Read and page INT traces at the collector before transferring their JSON."""

from __future__ import annotations

import json
import sys
import time
from collections import deque

INCOMPLETE_GRACE_NS = 2_000_000_000


def collapse_traces(
    records, now_ns: int, grace_ns: int = INCOMPLETE_GRACE_NS
) -> list[dict]:
    """Merge packet observations and retain the original in-flight grace period."""
    latest: dict[tuple[str, str], dict] = {}
    for row in records:
        key = (str(row.get("flow_id")), str(row.get("packet_id")))
        previous = latest.get(key)
        arrivals = sorted(
            set(row.get("arrival_switches", []))
            | set((previous or {}).get("arrival_switches", []))
        )
        if previous is None or (
            bool(row.get("sink_seen")),
            len(row.get("hop_sequence", [])),
            int(row["packet_timestamp"]),
        ) >= (
            bool(previous.get("sink_seen")),
            len(previous.get("hop_sequence", [])),
            int(previous["packet_timestamp"]),
        ):
            latest[key] = row
        if arrivals:
            latest[key] = {**latest[key], "arrival_switches": arrivals}
    return [
        row
        for row in latest.values()
        if row.get("trace_complete")
        or now_ns - int(row["packet_timestamp"]) >= grace_ns
    ]


def records_from_lines(lines):
    for line in lines:
        try:
            row = json.loads(line)
            int(row["packet_timestamp"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        yield row


def query_records(
    records,
    now_ns: int,
    *,
    start_ns: int,
    end_ns: int | None = None,
    filters: dict | None = None,
    egress_switch: str | None = None,
    egress_port: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Collapse before filtering so ingress-only reports still prove delivery."""
    if offset < 0:
        raise ValueError("offset must be nonnegative")
    rows = []
    for row in collapse_traces(records, now_ns):
        stamp = int(row["packet_timestamp"])
        if stamp < start_ns or (end_ns is not None and stamp > end_ns):
            continue
        if any(
            value is not None and str(row.get(key)) != str(value)
            for key, value in (filters or {}).items()
        ):
            continue
        if (egress_switch is not None or egress_port is not None) and not any(
            (egress_switch is None or hop.get("switch") == egress_switch)
            and (egress_port is None or hop.get("egress_port") == egress_port)
            for hop in row.get("hop_sequence", [])
        ):
            continue
        rows.append(row)
    stop = max(0, len(rows) - offset)
    return rows[max(0, stop - max(1, min(limit, 1000))) : stop]


def main() -> None:
    query = json.loads(sys.argv[1])
    path = query.pop("path")
    now_ns = time.time_ns()
    # Match the previous tail window without transporting that raw window.
    with open(path, encoding="utf-8", errors="replace") as source:
        lines = deque(source, maxlen=100_000)
    rows = query_records(records_from_lines(lines), now_ns, **query)
    print(json.dumps(rows, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
