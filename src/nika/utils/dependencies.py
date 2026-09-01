"""Helpers for optional dependency extras."""

from __future__ import annotations

from typing import NoReturn

_EXTRA_HINTS: dict[str, str] = {
    "kathara": (
        "Kathara support requires the 'kathara' extra. "
        "Install with: uv sync --extra kathara   # or: uv sync --extra labs"
    ),
    "containerlab": (
        "Containerlab support requires the 'containerlab' extra (Docker SDK). "
        "Also install the external `clab` binary (and `gnmic` for SRL scenarios). "
        "Install with: uv sync --extra containerlab   # or: uv sync --extra labs"
    ),
    "docker": (
        "Docker SDK support requires a lab extra. Install with: uv sync --extra labs"
    ),
    "kubernetes": (
        "Kubernetes support requires the 'kathara' extra. "
        "Install with: uv sync --extra kathara   # or: uv sync --extra labs"
    ),
}


def missing_extra_message(extra: str) -> str:
    """Return the install hint for a missing optional lab extra."""
    return _EXTRA_HINTS.get(
        extra,
        f"Missing optional dependency for '{extra}'. Install with: uv sync --extra labs",
    )


def raise_missing_extra(extra: str, *, cause: BaseException | None = None) -> NoReturn:
    """Raise ModuleNotFoundError with an install hint for *extra*."""
    raise ModuleNotFoundError(missing_extra_message(extra)) from cause


def require_backend_extra(backend: str) -> None:
    """Ensure the Python packages for *backend* are importable."""
    if backend == "containerlab":
        try:
            import docker  # noqa: F401
        except ImportError as exc:
            raise_missing_extra("containerlab", cause=exc)
        return

    if backend == "kathara":
        try:
            import Kathara  # noqa: F401
        except ImportError as exc:
            raise_missing_extra("kathara", cause=exc)
        return

    raise ValueError(f"Unknown lab backend: {backend!r}")
