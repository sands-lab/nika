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


def routeros_bgp_received_notification(output: str) -> str | None:
    """Decode the input session's NOTIFICATION, including optional RFC 4486 data."""
    section = re.search(
        r"(?:^|\s)input\.(.*?)(?=\s(?:remote|local|output)\.|\Z)",
        output,
        re.S,
    )
    if section is None:
        return None
    match = re.search(
        r"(?:^|\s)(?:input\.|\.)?last-notification=([0-9a-fA-F]+)\b",
        section[1],
    )
    if match is None or len(match[1]) % 2:
        return None
    message = bytes.fromhex(match[1])
    if len(message) < 21 or message[:16] != b"\xff" * 16 or message[18] != 3:
        return None
    if message[19:21] == b"\x06\x01":
        return "Cease/Maximum Number of Prefixes Reached"
    return f"BGP error {message[19]}/{message[20]}"
