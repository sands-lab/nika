"""Advanced: run benchmark trials in Firecracker microVMs (``nika.execution``).

A *lab image* per environment variant holds a deployed, verified lab: a
Firecracker snapshot of a VM (memory, device state and disk). Each trial
restores a disposable copy of it, rebinds the lab session to the trial, and
drives it through the NIKA remote daemon in the VM. See
docs/operations/firecracker.md.
"""

from nika.firecracker.config import firecracker_enabled

__all__ = ["firecracker_enabled"]
