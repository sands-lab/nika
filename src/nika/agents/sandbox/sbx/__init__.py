"""Docker Sandboxes (sbx) backend for NIKA agent execution."""

__all__ = [
    "SbxBackend",
    "sbx_available",
]


def __getattr__(name: str):
    if name == "sbx_available":
        from nika.agents.sandbox.sbx.client import sbx_available

        return sbx_available
    if name == "SbxBackend":
        from nika.agents.sandbox.sbx.backend import SbxBackend

        return SbxBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
