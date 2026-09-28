"""Run-config lab settings shared by lab runtime backends."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nika.run_config.schema import LabSettings


def lab_settings() -> LabSettings:
    """Return ``nika.lab`` from the run config, or defaults when unavailable."""
    from nika.run_config.loader import get_run_config
    from nika.run_config.schema import LabSettings as _LabSettings

    try:
        return get_run_config().nika.lab
    except Exception:  # noqa: BLE001
        return _LabSettings()
