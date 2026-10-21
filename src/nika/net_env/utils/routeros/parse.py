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
    return [
        routeros_properties(line)
        for line in output.splitlines()
        if re.match(r"\s*\d+\s", line)
    ]
