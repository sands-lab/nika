"""Access to ``nika.snapshot`` settings."""

from __future__ import annotations

from nika.run_config.schema import SnapshotSettings


def snapshot_settings() -> SnapshotSettings:
    from nika.run_config.loader import get_run_config

    return get_run_config().nika.snapshot


def snapshot_enabled() -> bool:
    """True when benchmark trials should run from VM snapshots."""
    try:
        return snapshot_settings().enabled
    except Exception:  # noqa: BLE001 - a broken run config must not enable it
        return False
