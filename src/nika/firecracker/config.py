"""Access to the ``nika.execution`` settings and the Firecracker root."""

from __future__ import annotations

import errno
import functools
import os
from pathlib import Path

from nika.run_config.schema import FirecrackerSettings

# Data that compresses well (VM memory and disks); trial copies are written
# while a lab runs, so they stay uncompressed.
_COMPRESSED = ("", "base", "images", "cases")
_UNCOMPRESSED = ("trials",)


def firecracker_settings() -> FirecrackerSettings:
    from nika.run_config.loader import get_run_config

    return get_run_config().nika.execution.firecracker


def firecracker_enabled() -> bool:
    """True when benchmark trials run in Firecracker (``nika.execution.mode``)."""
    try:
        from nika.run_config.loader import get_run_config

        return get_run_config().nika.execution.mode == "firecracker"
    except Exception:  # noqa: BLE001 - a broken run config must not enable it
        return False


def firecracker_root() -> Path:
    """The Firecracker root (relative paths are under the NIKA checkout)."""
    from nika.config import REPO_ROOT

    path = Path(firecracker_settings().root)
    return _prepared(path if path.is_absolute() else REPO_ROOT / path)


@functools.cache
def _prepared(root: Path) -> Path:
    """Create ``root`` and turn on btrfs zstd compression for its image data.

    Snapshots hold VM memory, which zstd shrinks about 3x on disk (and reads
    from disk faster). btrfs applies a directory's compression to files created
    in it later; other filesystems do not support it and stay as they are.
    """
    settable = True
    for name, value in [(n, b"zstd") for n in _COMPRESSED] + [
        (n, b"none") for n in _UNCOMPRESSED
    ]:
        path = root / name
        path.mkdir(parents=True, exist_ok=True)
        if not settable:
            continue
        try:
            os.setxattr(path, "btrfs.compression", value)
        except OSError as exc:
            if exc.errno not in (errno.EOPNOTSUPP, errno.ENOTSUP, errno.EPERM):
                raise
            settable = False  # not btrfs (or not ours): nothing to set
    return root
