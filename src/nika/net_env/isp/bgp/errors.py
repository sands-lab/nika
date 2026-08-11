"""Typed errors for ISP BGP compilation."""

from __future__ import annotations


class BgpError(Exception):
    """Base error for ISP BGP failures."""

    def __init__(self, message: str, *, topology: str | None = None) -> None:
        self.topology = topology
        prefix = f"[{topology}] " if topology else ""
        super().__init__(f"{prefix}{message}")


class BgpConfigError(BgpError):
    """Invalid BGP mode or option."""


class BgpCompileError(BgpError):
    """ISP plan cannot be compiled into a BGP plan."""
