"""Device CLI commands, parsed and quoted before execution by MCP tools."""

from __future__ import annotations

import re
import shlex
from itertools import groupby

MAX_OUTPUT = 65536
COMMAND_TIMEOUT = 10
TIMEOUT_MARKER = "NIKA_DEVICE_READ_TIMEOUT"

# Summarize a complete table before applying the transport byte limit. Keep a
# bounded prefix of ordinary rows alongside the exact reply-port inventory.
_CONNTRACK_PROJECTION = r"""
{
    bytes += length($0) + 1
    if (length(rows) <= maximum) rows = rows $0 "\n"
    destination = port = ""; destinations = ports = 0
    for (i = 1; i <= NF; i++) {
        if ($i ~ /^dst=/ && ++destinations == 2) destination = substr($i, 5)
        if ($i ~ /^dport=/ && ++ports == 2) port = substr($i, 7)
    }
    if (destination != "" && port ~ /^[0-9]+$/ && port <= 65535) {
        addresses[destination] = 1
        occupied[destination, port + 0] = 1
    }
}
END {
    if (bytes <= maximum || length(addresses) == 0) {
        printf "%s", rows
        exit
    }
    for (address in addresses) {
        printf "REPLY_DST_PORTS %s [", address
        separator = ""
        for (port = 0; port <= 65535; port++) {
            if (!((address, port) in occupied)) continue
            first = port
            while ((address, port + 1) in occupied) port++
            printf "%s%d", separator, first
            if (port > first) printf "-%d", port
            separator = ","
        }
        print "]"
    }
    printf "%s", substr(rows, 1, retained)
}
"""


def words(command: str) -> list[str]:
    """Parse one device program; execution quotes every argument."""
    if not command or "\x00" in command:
        raise ValueError("A device command is required")
    tokens = shlex.split(command)
    if not tokens:
        raise ValueError("A device command is required")
    return tokens


def linux_read_argv(command: str) -> list[str]:
    """Parse the Linux network device CLI, including configuration commands."""
    argv = words(command)
    if argv[0] not in {
        "ip",
        "bridge",
        "tc",
        "nft",
        "ss",
        "ethtool",
        "wg",
        "conntrack",
        "ping",
        "traceroute",
    }:
        raise ValueError("Expected a Linux network device CLI program")
    return argv


def bounded_command(argv: list[str]) -> str:
    """Bound device-side runtime and bytes before the host API buffers output."""
    pipeline = f"{shlex.join(argv)} 2>&1"
    if argv[:2] == ["conntrack", "-L"]:
        from nika.mcp.tool_output import MAX_CHARS

        pipeline += (
            f" | awk -v maximum={MAX_OUTPUT} -v retained={MAX_CHARS} "
            f"{shlex.quote(_CONNTRACK_PROJECTION)}"
        )
    pipeline += f" | head -c {MAX_OUTPUT + 1}"
    return (
        f"timeout -s KILL {COMMAND_TIMEOUT} sh -c {shlex.quote(pipeline)}; "
        "_nika_read_status=$?; "
        'if [ "$_nika_read_status" -eq 137 ] || [ "$_nika_read_status" -eq 124 ]; '
        f"then printf '\n{TIMEOUT_MARKER}\n'; fi"
    )


def device_read_supported(identity, argv: list[str]) -> bool:
    from nika.runtime.spec import NodeRole

    if identity is None or identity.role not in {NodeRole.ROUTER, NodeRole.SWITCH}:
        return False
    caps = set(identity.capabilities)
    if argv[0] in {"ovs-ofctl", "ovs-vsctl"}:
        return "ovs" in caps
    if argv[0] == "vtysh":
        return bool(caps & {"frr", "sonic"})
    return (
        identity.role == NodeRole.ROUTER
        and "linux" in caps
        and not caps
        & {
            "bmv2",
            "p4runtime",
            "ovs",
            "sonic",
            "srl",
            "nokia_srlinux",
            "iosxr",
            "routeros",
        }
    )


def require_device(host_name: str, argv: list[str]) -> None:
    from nika.mcp.session_context import get_session_meta
    from nika.problems.rca.inventory import load_session_offline_net_env

    env = load_session_offline_net_env(get_session_meta())
    if not device_read_supported(env.machine_identities.get(host_name), argv):
        raise ValueError("This tool requires a device with the declared capability")


def device_read_argv(command: str) -> tuple[str, list[str]]:
    argv = words(command)
    if argv[0] == "vtysh":
        return "frr", argv
    if argv[0] in {"ovs-ofctl", "ovs-vsctl"}:
        return "ovs", argv
    return "frr", linux_read_argv(command)


def conntrack_display(output: str) -> str:
    """Render session tuples compactly so a small NAT pool fits one observation."""
    from nika.mcp.tool_output import MAX_CHARS

    rows = []
    reply_ports: dict[str, set[int]] = {}
    for line in output.splitlines():
        tuples = re.findall(r"src=(\S+) dst=(\S+) sport=(\d+) dport=(\d+)", line)
        if len(tuples) != 2:
            rows.append(line)
            continue
        original, reply = tuples
        reply_ports.setdefault(reply[1], set()).add(int(reply[3]))
        state = re.search(
            r"\b(ESTABLISHED|SYN_SENT|SYN_RECV|TIME_WAIT|CLOSE|CLOSE_WAIT|FIN_WAIT|LAST_ACK)\b",
            line,
        )
        rows.append(
            f"{state[1] if state else 'TCP'} {original[0]}:{original[2]} -> {original[1]}:{original[3]} => {reply[1]}:{reply[3]}"
        )
    rendered = "\n".join(rows)
    if len(rendered) <= MAX_CHARS or any(
        line.startswith("REPLY_DST_PORTS ") for line in output.splitlines()
    ):
        return rendered
    summaries = []
    for address, ports in sorted(reply_ports.items()):
        ranges = []
        for _, group in groupby(
            enumerate(sorted(ports)), lambda pair: pair[1] - pair[0]
        ):
            run = [port for _, port in group]
            ranges.append(str(run[0]) if len(run) == 1 else f"{run[0]}-{run[-1]}")
        summaries.append(f"REPLY_DST_PORTS {address} [{','.join(ranges)}]")
    return "\n".join([*summaries, rendered])
