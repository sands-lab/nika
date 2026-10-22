"""Parse RouterOS CLI ``print`` output into property mappings."""

from __future__ import annotations

import re

_CLI_ERROR = re.compile(
    r"syntax error|bad command|expected end of command|no such item|failure:", re.I
)


def routeros_properties(output: str) -> dict[str, str]:
    """Return ``key=value`` properties; ``.key`` continues the previous group."""
    return {
        key.lstrip("."): quoted or bare
        for key, quoted, bare in re.findall(
            r'(?:^|\s)([\w.-]+)=(?:"([^"\n]*)"|([^\s]+))', output
        )
    }


def routeros_terse_rows(output: str) -> list[dict[str, str]]:
    """Parse ``print terse`` output, one item per line.

    Raises ``ValueError`` on CLI errors so callers do not read a rejected
    command as an empty table.
    """
    if _CLI_ERROR.search(output):
        raise ValueError(f"RouterOS CLI error: {output.strip()}")
    # Most menus number their items; /routing/route prints only flags.
    return [
        routeros_properties(line)
        for line in output.splitlines()
        if "=" in line and not line.lstrip().startswith(("Flags:", "Columns:"))
    ]


def routeros_established_bgp_peers(output: str) -> set[str]:
    """Return remote addresses from ``/routing/bgp/session/print terse``.

    Run the command with ``where established=yes``; RouterOS shows the
    session state as a flag, not as a property. Errors yield no peers.
    """
    try:
        rows = routeros_terse_rows(output)
    except ValueError:
        return set()
    # Sessions in a VRF report ``address@vrf``.
    return {
        row["remote.address"].split("@", 1)[0]
        for row in rows
        if "remote.address" in row
    }


def routeros_count(output: str) -> int:
    """Read ``print count-only`` output; errors and timeouts count as zero."""
    value = output.strip()
    return int(value) if value.isdigit() else 0
