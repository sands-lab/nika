"""Access to the ``nika.execution`` mode and ``nika.firecracker`` settings."""

from __future__ import annotations

from nika.run_config.schema import FirecrackerSettings


def firecracker_settings() -> FirecrackerSettings:
    from nika.run_config.loader import get_run_config

    return get_run_config().nika.firecracker


def firecracker_enabled() -> bool:
    """True when benchmark trials run in Firecracker (``nika.execution``)."""
    try:
        from nika.run_config.loader import get_run_config

        return get_run_config().nika.execution == "firecracker"
    except Exception:  # noqa: BLE001 - a broken run config must not enable it
        return False
