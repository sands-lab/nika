"""Map authored Oracle reads to the declared device access surface."""

from __future__ import annotations

import re
import shlex
from dataclasses import replace

from nika.mcp.device_read import device_read_argv, device_read_supported
from nika.problems.support.oracle import OraclePlaybook, OracleStep
from nika.runtime.spec import NodeRole


def device_commands(command: str) -> list[str]:
    """Translate authored shell reads; unsupported commands remain audit failures.

    Display filters and stderr redirects are unnecessary on bounded read tools.
    This translator handles trusted playbooks, never incoming agent commands.
    The resulting commands still pass the device tool's strict parser.
    """
    command = re.sub(r"\s+2>\s*(?:&1|/dev/null)", "", command)
    command = re.sub(r"\s*\|\|\s*true\b", "", command)
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";|")
    lexer.whitespace_split = True
    lexer.commenters = ""
    chunks: list[list[str]] = [[]]
    filtering = False
    filter_start = False
    for token in lexer:
        if token == ";":
            chunks.append([])
            filtering = False
        elif token == "|":
            filtering = True
            filter_start = True
        elif filtering:
            if filter_start:
                if token not in {"grep", "head", "tail"}:
                    raise ValueError("Playbook transforms evidence in a shell")
                filter_start = False
            # Only display filters may be removed. awk often creates fields
            # consumed by a later check, so it needs an authored CLI playbook.
            if token in {"awk", "sed", "python", "python3", "sh"}:
                raise ValueError("Playbook transforms evidence in a shell")
        else:
            chunks[-1].append(token)
    commands = []
    for chunk in chunks:
        if not chunk:
            continue
        if chunk[0] in {"ip", "bridge"}:
            flags = [x for x in chunk[1:] if x.startswith("-")]
            args = [x for x in chunk[1:] if not x.startswith("-")]
            if len(args) == 1 or len(args) > 1 and args[1] not in {"show", "list"}:
                args.insert(1, "show")
            chunk = [chunk[0], *flags, *args]
        text = shlex.join(chunk)
        device_read_argv(text)
        commands.append(text)
    if not 1 <= len(commands) <= 8:
        raise ValueError("Unsupported number of device reads")
    return commands


def device_step(step: OracleStep, net_env) -> OracleStep:
    """Route one resolved command through the declared device tools."""
    if step.tool != "exec_shell" or callable(step.args):
        return step
    node = step.args.get("host_name")
    identity = net_env.machine_identities.get(node)
    if identity is None or identity.role not in {NodeRole.ROUTER, NodeRole.SWITCH}:
        return step
    try:
        commands = device_commands(str(step.args["command"]))
        if any(
            not device_read_supported(identity, device_read_argv(c)[1])
            for c in commands
        ):
            return step
    except ValueError:
        return step
    return replace(
        step, tool="device_read", args={"host_name": node, "commands": commands}
    )


def device_playbook(playbook: OraclePlaybook, net_env) -> OraclePlaybook:
    """Map authored reads while retaining every evidence check.

    Unsupported steps remain visible and fail at the gateway. Commands built
    from prior outputs are mapped after their arguments resolve at execution.
    """
    return replace(
        playbook, steps=[device_step(step, net_env) for step in playbook.steps]
    )
