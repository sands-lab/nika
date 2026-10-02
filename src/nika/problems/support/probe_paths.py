"""Scenario default endpoint paths used by inject helpers and tests."""

from __future__ import annotations

from nika.net_env.base import ProbePath

__all__ = ["ProbePath", "get_probe_path"]


def get_probe_path(scenario: str, *, topo_size: str = "s") -> ProbePath | None:
    """Default probe path declared by ``scenario`` (``None`` when unavailable)."""
    from nika.net_env.net_env_pool import scenario_probe_path

    try:
        return scenario_probe_path(scenario, topo_size=topo_size)
    except Exception:  # noqa: BLE001
        return None
