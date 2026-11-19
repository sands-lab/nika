"""P4 gateway fault adapters: P4Runtime writes and switch route geometry."""

from __future__ import annotations

from nika.problems.support.p4runtime_writes import p4runtime_write, require_ok
from nika.runtime.base import LabRuntime


def _modify_port_entry(
    runtime: LabRuntime,
    switch: str,
    table: str,
    field: str,
    port: int,
    action: str,
    params: dict[str, int],
) -> dict:
    spec = {
        "type": "MODIFY",
        "table": table,
        "match": {field: int(port)},
        "action": action,
        "params": params,
    }
    return require_ok(
        p4runtime_write(runtime, switch, [spec]), f"{table} update on {switch}"
    )


def set_ecn_threshold(
    runtime: LabRuntime, switch: str, port: int, threshold: int
) -> dict:
    return _modify_port_entry(
        runtime,
        switch,
        "ecn_config",
        "egress_port",
        port,
        "set_ecn_threshold",
        {"threshold": int(threshold)},
    )


def set_icmp_frag_needed_filter(runtime: LabRuntime, switch: str) -> dict:
    spec = {
        "table": "icmp_acl",
        "match": {"type": 3, "code": 4},
        "action": "drop",
    }
    return require_ok(
        p4runtime_write(runtime, switch, [spec]), f"ICMP filter on {switch}"
    )
