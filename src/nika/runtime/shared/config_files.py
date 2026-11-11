"""Refresh saved configuration metadata without changing file contents."""

from __future__ import annotations

import shlex


def rewrite_saved_config_files(
    exec_cmd, nodes, directory: str, pattern: str, timestamp: float
) -> None:
    """Recreate a platform's saved files on every peer, then align modification times."""
    from nika.utils.parallel import bounded_parallel_map

    command = (
        f"set -e; directory={shlex.quote(directory)}; pattern={shlex.quote(pattern)}; "
        'for f in "$directory"/$pattern; do [ -f "$f" ] || continue; '
        'cp -p "$f" "$f.new"; mv -f "$f.new" "$f"; done; '
        f'touch -c -d @{int(timestamp)} "$directory" "$directory"/$pattern'
    )
    bounded_parallel_map(lambda node: exec_cmd(node, command), list(nodes))
