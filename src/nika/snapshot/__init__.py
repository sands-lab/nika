"""Advanced: run benchmark trials from stateful Incus VM snapshots of labs.

A *golden* VM per environment variant holds a deployed, verified lab in a
stateful snapshot. Each trial restores a disposable copy of it, rebinds the lab
session to the trial, and drives it through the NIKA remote daemon in the VM.
Enabled with ``nika.snapshot.enabled``; see docs/operations/vm-snapshots.md.
"""

from nika.snapshot.config import snapshot_enabled

__all__ = ["snapshot_enabled"]
