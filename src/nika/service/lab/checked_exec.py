"""Checked observations over lab APIs that return command errors as text."""

from __future__ import annotations

import shlex
from collections.abc import Callable


def checked_exec(
    execute: Callable[..., str],
    node: str,
    command: str,
    *,
    timeout: float = 10,
) -> str:
    """Use the nft exit-status marker pattern and preserve successful stdout."""
    script = (
        "err_file=$(mktemp) || exit 1; "
        "trap 'rm -f \"$err_file\"' EXIT; "
        f'({command}) 2>"$err_file"; rc=$?; '
        'if [ "$rc" -ne 0 ]; then cat "$err_file"; fi; '
        "printf '\n__rc=%s\n' \"$rc\""
    )
    raw = execute(node, f"sh -c {shlex.quote(script)}", timeout=timeout)
    output, marker, status = raw.rpartition("__rc=")
    try:
        returncode = int(status.strip()) if marker else None
    except ValueError:
        returncode = None
    if returncode is None:
        raise RuntimeError(f"Observation on {node} returned no exit status: {raw}")
    if returncode != 0:
        raise RuntimeError(
            f"Observation on {node} failed (rc={returncode}): {output.strip()}"
        )
    return output.strip()
