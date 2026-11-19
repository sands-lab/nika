"""BMv2 / P4Runtime operator helpers for Kathara P4 scenarios."""

from __future__ import annotations

import json
import shlex

from nika.net_env.p4_dc_fabric.fabric_manager.apply import MANAGER
from nika.service.kathara.base_api import KatharaBaseAPI, _SupportsBase

P4RT_HELP = """\
usage: p4rt_exec "<command> [options]"

P4Runtime device reads and writes from the fabric controller (fabric_mgr).

commands:
  probe --switch NAME                  controller and switch P4Runtime liveness
  read [--switch NAME]                  pipeline, tables, counters and registers
  read-table --switch NAME --table T    every entry of one P4 table
  counters [--switch NAME]              ingress/egress port and flow counters
  apply [--switch NAME]                install the pipeline and original intent
  delete-lpm --switch NAME --prefix P  delete a forwarding prefix
  insert|modify|delete --switch NAME --table T [--match field=value ...]
      [--action A --param name=value ...] [--priority N] [--default]
      [--member-id N | --group-id N]

Match values: exact value, IPv4 prefix, value&&&mask, or low..high.
Omit optional, ternary or LPM fields to match any value.
"""

LOCK_FILE = "/tmp/p4rt_exec.lock"
LOCK_WAIT_S = 80


def p4rt_command(args: str) -> str | None:
    """Quote manager arguments at the controller shell boundary."""
    words = shlex.split(args)
    if not words or words == ["help"]:
        return None
    argv = shlex.join(words)
    # Serialize manager runs so one command cannot preempt another's
    # P4Runtime mastership.
    return f"flock -w {LOCK_WAIT_S} {LOCK_FILE} python3 {MANAGER} {argv}"


class BMv2APIMixin:
    """Run P4Runtime commands through the fabric controller."""

    def p4rt_exec(self: _SupportsBase, args: str, timeout: float = 90) -> str:
        """Run a ``p4rt_manager.py`` command on fabric_mgr."""
        try:
            command = p4rt_command(args)
        except ValueError as exc:
            return f"error: {exc}\n\n{P4RT_HELP}"
        if command is None:
            return P4RT_HELP
        raw = self.exec_cmd("fabric_mgr", command, timeout=timeout)
        try:
            return json.dumps(json.loads(raw), indent=2)
        except ValueError:
            return raw


class KatharaBMv2API(KatharaBaseAPI, BMv2APIMixin):
    """Kathara API for live BMv2 P4Runtime access."""

    pass
