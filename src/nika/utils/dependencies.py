"""Helpers for optional lab dependencies."""

from __future__ import annotations

import shutil
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

GNMIC_INSTALL_HINT = (
    "Nokia SR Linux Containerlab labs require the host `gnmic` binary "
    "(min3clos and isp_* with --backend containerlab / nokia_srlinux). "
    "Install: https://gnmic.openconfig.net/install/ "
    'or `bash -c "$(curl -sL https://get-gnmic.openconfig.net)"`'
)


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


def require_gnmic() -> None:
    """Fail fast when the host ``gnmic`` CLI is missing (SRL Containerlab labs)."""
    if shutil.which("gnmic"):
        return
    raise FileNotFoundError(GNMIC_INSTALL_HINT)
