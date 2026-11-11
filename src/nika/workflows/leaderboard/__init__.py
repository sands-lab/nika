"""Leaderboard APIs; read-only audits do not load optional runtime backends."""

from importlib import import_module

_EXPORTS = {
    "PackResult": "pack",
    "ValidationReport": "validate",
    "pack_leaderboard_submission": "pack",
    "sibling_trajectories_dir": "validate_trajectories",
    "submit_leaderboard_package": "submit",
    "validate_leaderboard_submission": "validate",
    "validate_trajectory_package": "validate_trajectories",
    "write_submission_templates": "meta_input",
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(name)
    value = getattr(import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value
