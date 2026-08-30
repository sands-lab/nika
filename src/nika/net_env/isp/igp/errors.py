"""Typed errors for ISP compilation."""

from __future__ import annotations


class IspError(Exception):
    """Base error for ISP compile / config failures."""

    def __init__(self, message: str, *, topology: str | None = None) -> None:
        self.topology = topology
        prefix = f"[{topology}] " if topology else ""
        super().__init__(f"{prefix}{message}")


class IspConfigError(IspError):
    """Invalid ISP configuration or unsupported option combination."""


class IspCompileError(IspError):
    """Topology cannot be compiled into a runnable ISP plan."""
