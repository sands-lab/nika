"""Preserve configuration modification times across failure edits."""

from contextlib import contextmanager
import re
import shlex


@contextmanager
def preserve_mtime(runtime, node: str, path: str):
    """Restore a file's mtime after editing; ctime cannot be reset."""
    quoted = shlex.quote(path)
    epoch = runtime.exec(node, f"date -r {quoted} +%s.%N").strip()
    if not re.fullmatch(r"-?\d+\.\d{9}", epoch):
        raise RuntimeError(f"Cannot read modification time for {path} on {node}")
    try:
        yield
    finally:
        runtime.exec(node, f"touch -d @{epoch} -- {quoted}")


def refresh_peer_metadata(runtime, identities, target: str, paths) -> None:
    """Refresh matching peers without changing file contents or access/modification times."""
    from nika.service.lab.checked_exec import checked_exec

    identity = identities[target]
    for node, peer in sorted(identities.items()):
        if peer.role != identity.role or peer.capabilities != identity.capabilities:
            continue
        for path in paths:
            quoted = shlex.quote(path)
            checked_exec(
                runtime.exec,
                node,
                f"if [ -f {quoted} ]; then touch -c -r {quoted} -- {quoted}; fi",
                timeout=20,
            )
