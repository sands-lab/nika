"""FRR helpers shared by routing and security failures."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nika.runtime.base import LabRuntime

FRR_DIR = "/etc/frr"
FRR_CONF = f"{FRR_DIR}/frr.conf"


def frr_save_config(runtime: "LabRuntime", device: str) -> None:
    """Persist the running config to ``frr.conf`` without leaving a pre-fault copy.

    With ``service integrated-vtysh-config`` FRR renames the previous
    ``frr.conf`` to ``frr.conf.sav`` on ``write memory``. Split-config labs
    write ``bgpd.conf.sav`` the same way. Those files would diff straight to
    the injected change, so drop every ``.sav`` under ``/etc/frr`` after the write.
    """
    runtime.exec(
        device, f"vtysh -c 'write memory' >/dev/null 2>&1; rm -f {FRR_DIR}/*.sav"
    )


def frr_config_command(*lines: str) -> str:
    """One vtysh process that applies ``lines`` as a single configuration session.

    FRR 10 mgmtd commits every northbound change of a plain ``configure
    terminal`` on its own, so a multi-line change leaves a burst of commits
    milliseconds apart. A file-locked session stages the lines and applies
    them with one ``mgmt commit apply``, which is one commit-history entry like
    an operator's change.
    """
    parts = ["configure terminal file-lock", *lines, "mgmt commit apply", "end"]
    return "vtysh " + " ".join(f"-c {shlex.quote(part)}" for part in parts)
