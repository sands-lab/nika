"""Session metadata field access for runtime factory helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def meta_get(
    meta: dict[str, Any] | Any,
    key: str,
    *,
    scenario_params: bool = False,
    default: Any = None,
) -> Any:
    if isinstance(meta, dict):
        value = meta.get(key, default)
        if value is None and scenario_params:
            value = (meta.get("scenario_params") or {}).get(key, default)
        return value
    value = getattr(meta, key, default)
    if value is None and scenario_params:
        value = (getattr(meta, "scenario_params", None) or {}).get(key, default)
    return value


def meta_path(
    meta: dict[str, Any] | Any,
    key: str,
    *,
    scenario_params: bool = False,
) -> Path | None:
    raw = meta_get(meta, key, scenario_params=scenario_params)
    if not raw:
        return None
    return Path(str(raw))


def meta_lab_name(meta: dict[str, Any] | Any) -> str:
    lab_name = meta_get(meta, "lab_name")
    if not lab_name:
        raise ValueError("Session metadata has no lab_name.")
    return str(lab_name)


def device_profile_for(meta: dict[str, Any] | Any) -> str | None:
    """Return the router platform a session was started with, if it names one."""
    return meta_get(meta, "device_profile", scenario_params=True) or None
