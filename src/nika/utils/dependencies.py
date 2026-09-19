"""Helpers for optional lab dependencies."""

from __future__ import annotations

from typing import NoReturn

_LABS_HINT = "Install with: uv sync (installs the 'labs' dependency group by default)."

_EXTRA_HINTS: dict[str, str] = {
    "kathara": f"Kathara support requires the 'kathara' package. {_LABS_HINT}",
    "containerlab": (
        "Containerlab support requires the Docker SDK. "
        "Also install the external `clab` binary (and `gnmic` for SRL scenarios). "
        f"{_LABS_HINT}"
    ),
    "docker": f"Docker SDK support requires the 'docker' package. {_LABS_HINT}",
    "kubernetes": f"Kubernetes support requires the 'kubernetes' package. {_LABS_HINT}",
}


def missing_extra_message(extra: str) -> str:
    """Return the install hint for a missing optional lab dependency."""
    return _EXTRA_HINTS.get(
        extra,
        f"Missing optional dependency for '{extra}'. {_LABS_HINT}",
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
