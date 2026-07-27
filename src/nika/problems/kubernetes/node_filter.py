from __future__ import annotations

import ipaddress
import shlex
from dataclasses import dataclass
from typing import Any

FILTER_COMMENT = "nika-k8s-svc-block"
IPTABLES_CHAINS = ("PREROUTING", "OUTPUT")
IPTABLES_BINARY = "iptables"
FILTER_TIMEOUT_SEC: float = 30.0
WGET_BINARY = "wget"
PROBE_TIMEOUT_SEC: float = 3.0
_RC_MARK = "__NIKA_FILTER_RC="
_TIMEOUT_SENTINEL = "[TIMEOUT]"


class NodeFilterError(RuntimeError):
    """Raised when a node cannot be programmed with packet filter rules."""


def destination_forms(target: str) -> tuple[str, ...]:
    try:
        network = ipaddress.ip_network(target, strict=False)
    except ValueError:
        return (target,)
    if network.prefixlen == network.max_prefixlen:
        address = str(network.network_address)
        return (f"{address}/{network.max_prefixlen}", address)
    return (str(network),)


def _matches_destination(line: str, keyword: str, forms: tuple[str, ...]) -> bool:
    tokens = line.split()
    return any(
        token == keyword and tokens[index + 1] in forms
        for index, token in enumerate(tokens[:-1])
    )


def _matches_token_pair(line: str, keyword: str, value: str) -> bool:
    tokens = line.split()
    return any(
        token == keyword and tokens[index + 1] == value
        for index, token in enumerate(tokens[:-1])
    )


@dataclass(frozen=True)
class DropSpec:
    """A destination -- optionally narrowed to a protocol and port -- to drop.

    Without ``protocol``/``port`` this is an address-only drop (every packet to
    the destination); with them only that service is cut, which is what isolates
    e.g. DNS while leaving other traffic to the same address working.
    """

    destination: str
    protocol: str | None = None
    port: int | None = None

    @property
    def forms(self) -> tuple[str, ...]:
        return destination_forms(self.destination)

    def iptables_args(self, *, comment: str | None = None) -> str:
        args = f"-d {shlex.quote(self.destination)}"
        if self.protocol:
            args += f" -p {self.protocol}"
            if self.port is not None:
                args += f" --dport {self.port}"
        if comment:
            args += f" -m comment --comment {comment}"
        return f"{args} -j DROP"

    def matches_iptables_line(self, line: str) -> bool:
        if "-j DROP" not in line:
            return False
        if not _matches_destination(line, "-d", self.forms):
            return False
        if self.protocol and not _matches_token_pair(line, "-p", self.protocol):
            return False
        return self.port is None or _matches_token_pair(line, "--dport", str(self.port))

    def describe(self) -> str:
        if not self.protocol:
            return self.destination
        suffix = f":{self.port}" if self.port is not None else ""
        return f"{self.destination} {self.protocol}{suffix}"


@dataclass
class NodeFilter:
    """Install and inspect raw-table drops on a single lab device."""

    runtime: Any
    node: str
    timeout: float = FILTER_TIMEOUT_SEC

    def _exec(self, command: str) -> tuple[str, int]:
        raw = self.runtime.exec(
            self.node,
            f"( {command} ); printf '\\n{_RC_MARK}%s\\n' $?",
            timeout=self.timeout,
        )
        if raw.startswith(_TIMEOUT_SENTINEL) or _RC_MARK not in raw:
            raise NodeFilterError(
                f"Command {command!r} on {self.node!r} produced no exit-code marker "
                f"(timed out or shell unavailable): {raw[:200]!r}"
            )
        stdout, _, tail = raw.partition(_RC_MARK)
        try:
            returncode = int(tail.strip().splitlines()[0])
        except (IndexError, ValueError):
            returncode = 1
        return stdout.strip(), returncode

    def block_destination(self, target: str, *, protocol: str | None = None, port: int | None = None) -> None:
        self.block(DropSpec(target, protocol=protocol, port=port))

    def block(self, spec: DropSpec) -> None:
        self._block_with_iptables(spec)

        blocked = self.blocked_spec(spec)
        missing = [chain for chain, present in blocked.items() if not present]
        if missing:
            raise NodeFilterError(
                f"Failed to install raw drops for {spec.describe()!r} on "
                f"{self.node!r} using iptables; chains still unfiltered: "
                f"{', '.join(missing)}. Current ruleset: {self.rules_dump()[:500]!r}"
            )

    def _block_with_iptables(self, spec: DropSpec) -> None:
        binary = IPTABLES_BINARY
        for chain in IPTABLES_CHAINS:
            commented = spec.iptables_args(comment=FILTER_COMMENT)
            # The comment module is optional in stripped images: fall back to a
            # plain rule when it is unavailable.
            _, returncode = self._exec(
                f"{binary} -t raw -C {chain} {commented} 2>/dev/null || "
                f"{binary} -t raw -I {chain} 1 {commented}"
            )
            if returncode == 0:
                continue
            rule = spec.iptables_args()
            self._exec(
                f"{binary} -t raw -C {chain} {rule} 2>/dev/null || "
                f"{binary} -t raw -I {chain} 1 {rule}"
            )

    def rules_dump(self) -> str:
        stdout, _ = self._exec(f"{IPTABLES_BINARY} -t raw -S 2>/dev/null")
        return stdout

    def blocked(self, target: str, *, protocol: str | None = None, port: int | None = None) -> dict[str, bool]:
        return self.blocked_spec(DropSpec(target, protocol=protocol, port=port))

    def blocked_spec(self, spec: DropSpec) -> dict[str, bool]:
        return {
            chain.lower(): self._iptables_chain_blocks(chain, spec)
            for chain in IPTABLES_CHAINS
        }

    def _iptables_chain_blocks(self, chain: str, spec: DropSpec) -> bool:
        stdout, _ = self._exec(f"{IPTABLES_BINARY} -t raw -S {chain} 2>/dev/null")
        return any(spec.matches_iptables_line(line) for line in stdout.splitlines())

    def tcp_reachable(self, address: str, port: int) -> bool | None:
        timeout = int(PROBE_TIMEOUT_SEC)
        command = f"{WGET_BINARY} -q -T {timeout} -O /dev/null http://{address}:{port}/ >/dev/null 2>&1"
        _, returncode = self._exec(command)
        return returncode == 0
