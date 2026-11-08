"""SONiC CLI access for Containerlab SONiC-VS routers."""

from __future__ import annotations

import shlex

from nika.service.containerlab.base_api import ContainerlabBaseAPI
from nika.service.lab.protocols import SupportsExec

# Programs that make up the SONiC command line: the ``show`` and ``config``
# CLIs, FRR's shell, and the clients of SONiC's Redis databases.
SONIC_CLI_PROGRAMS = frozenset({"show", "config", "vtysh", "redis-cli", "sonic-db-cli"})
# Present on every SONiC image; its absence means the node is not a SONiC router.
SONIC_MARKER = "/usr/local/bin/sonic-cfggen"
_SHELL_OPERATORS = frozenset({"|", "||", "&", "&&", ";", ">", ">>", "<", "<<"})


def sonic_cli_command(command: str) -> str:
    """Return the shell line that runs one SONiC CLI *command*.

    The command must start with a SONiC CLI program and contain no shell
    operators; its arguments are quoted, so nothing else runs.
    """
    try:
        words = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"Cannot parse SONiC CLI command: {exc}.") from exc
    if not words or words[0] not in SONIC_CLI_PROGRAMS:
        raise ValueError(
            "SONiC CLI commands start with one of: "
            f"{', '.join(sorted(SONIC_CLI_PROGRAMS))}."
        )
    operators = sorted(_SHELL_OPERATORS.intersection(words))
    if operators:
        raise ValueError(
            "Run one SONiC CLI command without shell operators "
            f"({' '.join(operators)}); exec_shell runs shell pipelines."
        )
    return (
        f"if [ ! -e {SONIC_MARKER} ]; then echo 'Not a SONiC router.' >&2; "
        f"exit 127; fi; {shlex.join(words)}"
    )


def uses_sonic_router(runtime, device: str) -> bool:
    """Return True when ``device`` runs SONiC; ``runtime`` provides ``exec``."""
    output = runtime.exec(
        device, f"[ -e {SONIC_MARKER} ] && echo sonic || true", timeout=5
    )
    return "sonic" in (output or "")


class SonicAPIMixin:
    """Run SONiC CLI commands on SONiC-VS routers."""

    def sonic_exec_cli(
        self: SupportsExec,
        device_name: str,
        command: str,
        *,
        timeout: float = 60.0,
    ) -> str:
        """Run one SONiC CLI command on ``device_name`` and return its output."""
        return self.exec_cmd(device_name, sonic_cli_command(command), timeout=timeout)


class ContainerlabSonicAPI(ContainerlabBaseAPI, SonicAPIMixin):
    """Containerlab API with SONiC router operations."""

    def exec_cmd(self, host_name: str, command: str, timeout: float = 10) -> str:
        # runtime.exec already wraps commands in /bin/sh -c.
        return self.runtime.exec(host_name, command, timeout=timeout)
