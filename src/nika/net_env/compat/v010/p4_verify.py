"""Shared helpers for P4 scenario verification."""

from __future__ import annotations

from collections.abc import Iterable

from nika.net_env.verify import process_running
from nika.runtime.base import LabRuntime


def p4_switches_ready(runtime: LabRuntime, switches: Iterable[str]) -> bool:
    return all(process_running(runtime, switch, "simple_switch") for switch in switches)
