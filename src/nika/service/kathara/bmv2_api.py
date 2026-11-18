"""BMv2 / P4Runtime operator helpers for Kathara P4 scenarios."""

from __future__ import annotations

import argparse
import json
import re
import shlex

from nika.net_env.p4_dc_fabric.fabric_manager.apply import MANAGER
from nika.service.kathara.base_api import KatharaBaseAPI, _SupportsBase

P4RT_HELP = """\
usage: p4rt_exec "<command> [options]"

Read-only P4Runtime queries from the fabric controller (fabric_mgr).

commands:
  read [--switch NAME]                  pipeline, ActionSelector members and
                                        groups, ipv4_lpm, runtime tables,
                                        counters and registers, plus mismatches
                                        against the controller's intent
  read-table --switch NAME --table T    every entry of one P4 table
  counters [--switch NAME]              ingress/egress port and flow counters
"""

LOCK_FILE = "/tmp/p4rt_exec.lock"
LOCK_WAIT_S = 80
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # noqa: D401 - argparse hook
        raise _UsageError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="p4rt_exec", add_help=False, allow_abbrev=False)
    sub = parser.add_subparsers(dest="cmd", required=True, parser_class=_Parser)
    for name in ("read", "counters"):
        item = sub.add_parser(name, add_help=False, allow_abbrev=False)
        item.add_argument("--switch")
    table = sub.add_parser("read-table", add_help=False, allow_abbrev=False)
    table.add_argument("--switch", required=True)
    table.add_argument("--table", required=True)
    return parser


def p4rt_command(args: str) -> str | None:
    """Validate agent arguments; return the manager command or ``None`` for help.

    Only the read-only subcommands are accepted. Every value is checked and
    the command is rebuilt from the parsed fields, so no other manager option
    or subcommand can be passed through.
    """
    words = shlex.split(args)
    if not words or any(word in {"-h", "--help", "help"} for word in words):
        return None
    parsed = _parser().parse_args(words)
    argv = [parsed.cmd]
    for option in ("switch", "table"):
        value = getattr(parsed, option, None)
        if value is None:
            continue
        if not _NAME_RE.match(value):
            raise _UsageError(f"invalid --{option} value: {value!r}")
        argv += [f"--{option}", value]
    # P4Runtime master arbitration rejects a second client with the same
    # election id, so manager runs on fabric_mgr are serialized.
    return f"flock -w {LOCK_WAIT_S} {LOCK_FILE} python3 {MANAGER} {' '.join(argv)}"


class BMv2APIMixin:
    """Run read-only P4Runtime queries through the fabric controller."""

    def p4rt_exec(self: _SupportsBase, args: str, timeout: float = 90) -> str:
        """Run a read-only ``p4rt_manager.py`` query on fabric_mgr."""
        try:
            command = p4rt_command(args)
        except _UsageError as exc:
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
