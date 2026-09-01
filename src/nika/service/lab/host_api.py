"""Backend-neutral host API factory with lazy backend imports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nika.utils.dependencies import raise_missing_extra, require_backend_extra

if TYPE_CHECKING:
    from nika.runtime.base import LabRuntime


def create_host_api(
    *,
    lab_name: str,
    backend: str = "kathara",
    runtime: LabRuntime | None = None,
    session_meta: dict | None = None,
) -> Any:
    """Return KatharaBaseAPI or ContainerlabBaseAPI depending on backend."""
    if backend == "kathara":
        require_backend_extra("kathara")
        try:
            from nika.service.kathara.base_api import KatharaBaseAPI
        except ImportError as exc:
            raise_missing_extra("kathara", cause=exc)
        return KatharaBaseAPI(lab_name=lab_name, session_meta=session_meta)

    require_backend_extra("containerlab")
    try:
        from nika.service.containerlab.base_api import ContainerlabBaseAPI
    except ImportError as exc:
        raise_missing_extra("containerlab", cause=exc)

    if runtime is None:
        if session_meta is None:
            session_meta = {"lab_name": lab_name, "backend": backend}
        from nika.runtime.factory import runtime_for_session

        runtime = runtime_for_session(session_meta)
    return ContainerlabBaseAPI(runtime)
