"""Batch independent verification commands by node and phase.

Existing exec-based parsers read the collected observations. Callers perform
state changes through the runtime and collect fresh observations afterwards.
"""

from __future__ import annotations

import json
import re
import shlex
from collections import defaultdict
from dataclasses import dataclass
from time import perf_counter
from typing import Any
from uuid import uuid4

from nika.runtime.base import LabRuntime
from nika.service.pingmesh.parser import parse_ping_output
from nika.runtime.shared.execution import note_exec_failure
from nika.utils.parallel import bounded_parallel_map


@dataclass(frozen=True)
class VerifyCommand:
    node: str
    command: str
    timeout: float = 20.0
    required: bool = True


@dataclass(frozen=True)
class CommandResult:
    output: str
    returncode: int | None
    error: str | None = None
    duration_ms: float = 0.0
    required: bool = True


class VerifyCollection:
    """A phase's observations, read using the original command."""

    def __init__(
        self,
        results: dict[tuple[str, str], CommandResult],
        nodes: list[str],
        duration_ms: float,
    ) -> None:
        self.results = results
        self.nodes = nodes
        self.duration_ms = duration_ms
        self._read_errors: list[dict[str, str]] = []

    def list_nodes(self) -> list[str]:
        return list(self.nodes)

    def exec(self, node: str, command: str, *, timeout: float = 20.0) -> str:
        del timeout
        result = self.results.get((node, command))
        if result is None:
            self._read_errors.append(
                {"node": node, "command": command, "error": "uncollected command"}
            )
            raise ValueError(f"uncollected verification command on {node}: {command}")
        return "" if result.error else result.output

    def json(self, node: str, command: str) -> Any:
        result = self.results.get((node, command))
        if result is not None and result.error:
            raise ValueError(result.error)
        try:
            return json.loads(self.exec(node, command))
        except (ValueError, TypeError) as exc:
            self._read_errors.append(
                {"node": node, "command": command, "error": str(exc)}
            )
            raise

    @property
    def errors(self) -> list[dict[str, str]]:
        return self._read_errors + [
            {"node": node, "command": command, "error": result.error}
            for (node, command), result in self.results.items()
            if result.error
        ]

    @property
    def blocking_errors(self) -> list[dict[str, str]]:
        return [
            error
            for error in self.errors
            if (error["node"], error["command"]) not in self.results
            or self.results[error["node"], error["command"]].required
        ]

    def merged(self, later: VerifyCollection) -> VerifyCollection:
        combined = VerifyCollection(
            self.results | later.results,
            self.nodes,
            self.duration_ms + later.duration_ms,
        )
        combined._read_errors = self._read_errors + later._read_errors
        return combined

    def finish(self, result: dict[str, Any]) -> dict[str, Any]:
        """Attach collection evidence and prevent errors from passing isolation."""
        result["details"]["collection"] = {
            "nodes": sorted({node for node, _ in self.results}),
            "commands": len(self.results),
            "duration_ms": self.duration_ms,
            "errors": self.errors,
        }
        result["checks"]["observations_collected"] = not self.blocking_errors
        result["verified"] = bool(result["verified"]) and not self.blocking_errors
        if "failed" in result["details"]:
            result["details"]["failed"] = [
                key for key, value in result["checks"].items() if not value
            ]
        if "check_count" in result["details"]:
            result["details"]["check_count"] = len(result["checks"])
        return result


def ping_command(target: str, *, count: int = 1) -> str:
    return f"ping -c {count} -W 2 {target}"


def mesh_ping_command(target: str, *, count: int = 3, netns: str | None = None) -> str:
    prefix = f"ip netns exec {shlex.quote(netns)} " if netns else ""
    return f"{prefix}ping -c {count} -i 0.2 -W 2 -q {target}"


def http_command(url: str) -> str:
    return f"curl -s -o /dev/null -w '%{{http_code}}' --connect-timeout 5 {url}"


def collect_verify_commands(
    runtime: LabRuntime,
    commands: list[VerifyCommand],
    *,
    nodes: list[str] | None = None,
) -> VerifyCollection:
    """Batch independent reads/probes; call again after any state transition."""
    started = perf_counter()
    grouped: dict[str, dict[str, VerifyCommand]] = defaultdict(dict)
    for request in commands:
        previous = grouped[request.node].get(request.command)
        grouped[request.node][request.command] = (
            request
            if previous is None
            else VerifyCommand(
                request.node,
                request.command,
                min(previous.timeout, request.timeout),
                previous.required or request.required,
            )
        )

    def collect(item: tuple[str, dict[str, VerifyCommand]]):
        node, requests = item
        wanted = list(requests.values())
        nonce = "BATCH_" + uuid4().hex
        lines = [
            "probe_dir=$(mktemp -d) || exit 1",
            "trap 'rm -rf -- \"$probe_dir\"' EXIT HUP INT TERM",
        ]
        budget = 10.0
        for offset in range(0, len(wanted), 8):
            batch = wanted[offset : offset + 8]
            budget += max(request.timeout + 1 for request in batch)
            for index, request in enumerate(batch, offset):
                command = shlex.quote(request.command)
                lines.append(
                    f"(probe_start=$(date +%s%N); timeout -k 1 {request.timeout:g} sh -c {command} "
                    f'>"$probe_dir/{index}.out" 2>&1; '
                    "probe_rc=$?; probe_end=$(date +%s%N); "
                    f'echo "$probe_rc $(((probe_end - probe_start) / 1000000))" >"$probe_dir/{index}.rc") &'
                )
            lines.append("wait")
        for index in range(len(wanted)):
            lines.extend(
                [
                    f"printf '\\n{nonce} BEGIN {index} '",
                    f'cat "$probe_dir/{index}.rc"',
                    f'cat "$probe_dir/{index}.out"',
                    f"printf '\\n{nonce} END {index}\\n'",
                ]
            )
        try:
            raw = runtime.exec(node, "\n".join(lines), timeout=budget)
        except Exception as exc:  # noqa: BLE001 - retain other nodes' evidence
            raw = ""
            failure = str(exc)
        else:
            failure = raw if raw.startswith("[TIMEOUT]") else "incomplete batch output"
        matches = list(
            re.finditer(
                rf"(?:^|\n){nonce} BEGIN (\d+) (\d+) (\d+)\n(.*?)\n{nonce} END \1(?:\n|$)",
                raw,
                re.DOTALL,
            )
        )
        parsed: dict[int, CommandResult] = {}
        for match in matches:
            index, returncode = int(match[1]), int(match[2])
            error = None
            if returncode in {124, 125, 126, 127} or returncode >= 128:
                error = f"command exited {returncode}: {match[4].strip()}"
            if index < len(wanted):
                command = wanted[index].command
                if (
                    command.startswith("ping ")
                    or command.startswith("ip netns exec ")
                    and " ping " in command
                ) and parse_ping_output(match[4])["status"] == "unknown":
                    error = "unable to determine reachability from probe output"
            if index in parsed:
                error = "duplicate batch result"
            parsed[index] = CommandResult(
                match[4].strip(),
                returncode,
                error,
                float(match[3]),
                wanted[index].required if index < len(wanted) else True,
            )
        results = {}
        for index, request in enumerate(wanted):
            result = parsed.get(
                index, CommandResult("", None, failure, required=request.required)
            )
            results[(node, request.command)] = result
            if result.error:
                note_exec_failure(node, request.command, result.error)
        return results

    results = {}
    for node_results in bounded_parallel_map(collect, grouped.items()):
        results.update(node_results)
    return VerifyCollection(
        results,
        runtime.list_nodes() if nodes is None else nodes,
        (perf_counter() - started) * 1000,
    )
