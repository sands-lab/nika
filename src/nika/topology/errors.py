"""Typed errors for SNDlib topology import."""

from __future__ import annotations


class SndlibError(Exception):
    """Base error for SNDlib import failures."""

    def __init__(self, message: str, *, topology: str | None = None) -> None:
        self.topology = topology
        prefix = f"[{topology}] " if topology else ""
        super().__init__(f"{prefix}{message}")


class SndlibParseError(SndlibError):
    """Malformed or unreadable SNDlib input."""


class SndlibUnsupportedError(SndlibError):
    """SNDlib structure or format that NIKA does not support yet."""


class SndlibValidationError(SndlibError):
    """SNDlib network failed integrity / normalization checks."""
