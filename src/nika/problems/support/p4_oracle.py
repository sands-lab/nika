"""P4Runtime, BMv2, and INT telemetry steps for oracle playbooks of P4 failures."""

from __future__ import annotations

import json
from collections.abc import Callable

from nika.problems.support.oracle import Check, OracleStep, p4rt


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
    return OracleStep(
        f"Read port bindings and operational state on {switch}.",
        "bmv2_get_ports",
        {"switch": switch},
        expect,
    )


def summary_regex(switch: str, body: str) -> str:
    """Regex over *switch*'s entry in the ``read`` summary (no nested objects)."""
    return rf'"{switch}":\s*\{{[^{{}}]*?{body}'
