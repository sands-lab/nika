"""Shared argv-disguise helper for background traffic generators.

Agents can list processes on lab nodes (``ps``), so a generator's argv must
read like an ordinary client or monitoring agent rather than naming the
underlying tool. ``exec -a <name>`` rewrites a process's argv[0] without a
wrapper script, which keeps the literal tool name out of
``ps -eo args``/``ps aux`` output, but the kernel-recorded ``comm`` (seen in
``ps -o comm``, ``top``, ``pgrep``/``pkill`` without ``-f``) is set from the
basename of the path actually resolved by exec, not from argv[0]. So the
command run under ``exec -a`` must itself resolve to a neutrally named
binary. ``nika/base`` ships ``/usr/local/bin/latency-probe`` as a hardlink
to ``hping3`` (see ``Dockerfile.base``) for exactly this: callers invoke
``latency-probe`` instead of ``hping3``, which makes both argv and comm
read the same neutral name.
"""

from __future__ import annotations

# A plausible name for a host-local network-health/monitoring agent, and the
# neutral hardlink name for hping3 shipped in nika/base (Dockerfile.base).
# Real incidents of a misconfigured or runaway healthcheck/probe hammering a
# target are a well-documented failure mode, so this name reads as a
# believable source of unwanted SYN traffic rather than an attack tool.
NEUTRAL_PROBE_NAME = "latency-probe"


def exec_as(name: str, command: str) -> str:
    """Return a shell fragment that runs ``command`` with argv[0] set to ``name``."""
    return f"exec -a {name} {command}"
