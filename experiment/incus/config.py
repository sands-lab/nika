"""Select the packaged Incus base for this experiment process."""

from types import SimpleNamespace

_base: str | None = None


def configure(base: str) -> None:
    global _base
    if len(base.split("/")) != 2:
        raise ValueError("Use an Incus instance/snapshot as the base")
    _base = base


def snapshot_settings() -> SimpleNamespace:
    if _base is None:
        raise RuntimeError("Specify --incus-base before collecting Incus runs")
    return SimpleNamespace(base=_base, verify_after_restore=True)
