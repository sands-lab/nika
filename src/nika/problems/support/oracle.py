"""Reference diagnosis playbooks for the oracle solver.

A failure's ``oracle_playbook(params)`` returns the troubleshooting path an
operator follows to confirm that fault: ordered MCP tool calls, the observation
each call must show, and which observations prove the root cause. The oracle
agent (``nika.agents.oracle``) runs the steps through the same MCP tools a
diagnosis agent gets and submits the root cause only when every evidence step
holds, so a case the oracle cannot confirm is one whose fault is not
observable with the agent's tools.

The step builders below are the reusable templates; failure files
parameterize them with their injection parameters.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

Args = Mapping[str, Any] | Callable[[Mapping[str, str]], Mapping[str, Any]]


@dataclass(frozen=True)
class Check:
    """Expected observation in one tool result."""

    description: str
    predicate: Callable[[str], bool]

    def __call__(self, output: str) -> bool:
        return bool(self.predicate(output))


def contains(pattern: str, description: str | None = None) -> Check:
    """Output matches the case-insensitive multiline regex *pattern*."""
    rx = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    return Check(
        description or f"output matches /{pattern}/", lambda o: bool(rx.search(o))
    )


def lacks(pattern: str, description: str | None = None) -> Check:
    """Output does not match the case-insensitive multiline regex *pattern*."""
    rx = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    return Check(
        description or f"output does not match /{pattern}/",
        lambda o: not rx.search(o),
    )


def all_of(*checks: Check) -> Check:
    return Check(
        " and ".join(c.description for c in checks),
        lambda o: all(c(o) for c in checks),
    )


def numbers_after(label: str, output: str) -> list[float]:
    """Numbers that directly follow *label* (e.g. ``time=``) in *output*."""
    return [
        float(m)
        for m in re.findall(re.escape(label) + r"\s*([0-9]+(?:\.[0-9]+)?)", output)
    ]


@dataclass(frozen=True)
class OracleStep:
    """One troubleshooting action and the observation it must show.

    ``evidence`` steps prove the root cause; the oracle submits it only when
    all of them hold. Other steps record symptoms and context. ``args`` may be
    a callable of earlier outputs saved under ``save_as`` (for example a
    packet-capture ID). ``attempts`` re-runs the call until ``expect`` holds,
    for state that converges after injection.
    """

    purpose: str
    tool: str
    args: Args
    expect: Check | None = None
    evidence: bool = False
    save_as: str | None = None
    attempts: int = 1
    interval_sec: float = 5.0


@dataclass(frozen=True)
class OraclePlaybook:
    """Reference diagnosis path for one injected fault."""

    finding: str
    steps: list[OracleStep] = field(default_factory=list)
    anomaly: bool = True

    def __add__(self, other: OraclePlaybook) -> OraclePlaybook:
        return OraclePlaybook(
            finding=f"{self.finding} {other.finding}".strip(),
            steps=[*self.steps, *other.steps],
            anomaly=self.anomaly or other.anomaly,
        )


# ---------------------------------------------------------------------------
# Step templates
# ---------------------------------------------------------------------------


def triage() -> OracleStep:
    """Measure the user-visible symptom across the endpoint mesh."""
    return OracleStep(
        "Triage: measure reachability and latency across the endpoint mesh.",
        "run_pingmesh_snapshot",
        {},
    )


def shell(
    node: str,
    command: str,
    purpose: str,
    expect: Check | None = None,
    *,
    evidence: bool = False,
    timeout: float = 15,
    attempts: int = 1,
    interval_sec: float = 5.0,
    save_as: str | None = None,
) -> OracleStep:
    """Run *command* on lab node *node* with ``exec_shell``."""
    return OracleStep(
        purpose,
        "exec_shell",
        {"host_name": node, "command": command, "timeout": timeout},
        expect,
        evidence=evidence,
        attempts=attempts,
        interval_sec=interval_sec,
        save_as=save_as,
    )


def vtysh(*commands: str) -> str:
    """FRR ``vtysh`` invocation running *commands* in order."""
    return "vtysh " + " ".join(f"-c {shlex.quote(c)}" for c in commands)


def tcp_connect_cmd(address: str, port: int) -> str:
    """Shell probe that prints CONNECTED or NO_CONNECT for one TCP handshake."""
    return (
        f"timeout 4 busybox telnet {address} {port} </dev/null 2>&1 | "
        "grep -q Connected && echo CONNECTED || echo NO_CONNECT"
    )


def ping(
    src: str,
    dst: str,
    purpose: str,
    expect: Check | None = None,
    *,
    count: int = 3,
    evidence: bool = False,
    extra: str = "",
) -> OracleStep:
    """ICMP probe from *src* to *dst*."""
    return shell(
        src,
        f"ping -c {count} -W 1 {extra} {dst} 2>&1 | tail -4".replace("  ", " "),
        purpose,
        expect,
        evidence=evidence,
        timeout=count * 2 + 10,
    )


def ping_fails(
    src: str, dst: str, purpose: str, *, evidence: bool = False
) -> OracleStep:
    return ping(
        src,
        dst,
        purpose,
        contains(r"100% packet loss|unreachable|0 received", "every probe is lost"),
        evidence=evidence,
    )


def ping_works(
    src: str, dst: str, purpose: str, *, evidence: bool = False
) -> OracleStep:
    return ping(
        src,
        dst,
        purpose,
        contains(r"\b[1-9][0-9]* (packets )?received", "probes get replies"),
        evidence=evidence,
    )


def nft_rule(node: str, rule: str, purpose: str | None = None) -> OracleStep:
    """The node's nftables ruleset carries a drop rule matching *rule* (regex)."""
    return shell(
        node,
        "nft list ruleset 2>&1 | grep -nE 'drop|reject|hook|chain' | head -60",
        purpose or f"Inspect the packet filter on {node}.",
        contains(rule, f"a filter rule matching /{rule}/ is installed"),
        evidence=True,
    )


def iface_state(
    node: str, intf: str, expect: Check, purpose: str, *, evidence: bool = True
) -> OracleStep:
    return shell(
        node,
        f"ip -d link show dev {intf}; ip -br addr show dev {intf}",
        purpose,
        expect,
        evidence=evidence,
    )


def iface_down(
    node: str, intf: str, purpose: str | None = None, *, evidence: bool = True
) -> OracleStep:
    """*node*:*intf* has lost carrier or is administratively down."""
    return iface_state(
        node,
        intf,
        contains(
            r"state (DOWN|LOWERLAYERDOWN)", f"{node}:{intf} is operationally down"
        ),
        purpose or f"Check the operational state of {node}:{intf}.",
        evidence=evidence,
    )


def qdisc(node: str, intf: str, expect: Check, purpose: str) -> OracleStep:
    """Traffic-control qdiscs on *node*:*intf*."""
    return shell(
        node,
        f"tc -s qdisc show dev {intf}",
        purpose,
        expect,
        evidence=True,
    )


def _pgrep(pattern: str) -> str:
    """List processes whose name matches *pattern*.

    Prints ``NO_PROCESS`` only when pgrep runs and finds no match, and
    ``NO_PGREP`` when the node has no working pgrep.
    """
    return (
        f"pgrep -a {shlex.quote(pattern)}; rc=$?; "
        "[ $rc -eq 1 ] && echo NO_PROCESS; [ $rc -gt 1 ] && echo NO_PGREP; true"
    )


def process_absent(node: str, pattern: str, purpose: str) -> OracleStep:
    """No process named like *pattern* runs on *node*."""
    return shell(
        node,
        _pgrep(pattern),
        purpose,
        contains(r"^NO_PROCESS$", f"no {pattern} process is running"),
        evidence=True,
    )


def process_running(
    node: str, pattern: str, purpose: str, *, evidence: bool = False
) -> OracleStep:
    """A process named like *pattern* runs on *node*."""
    return shell(
        node,
        _pgrep(pattern),
        purpose,
        all_of(
            lacks(r"^NO_PROCESS$", f"a {pattern} process is running"),
            lacks(r"^NO_PGREP$", "pgrep works on the node"),
        ),
        evidence=evidence,
    )


def kubectl(
    node: str,
    args: str,
    purpose: str,
    expect: Check | None = None,
    *,
    evidence: bool = False,
    timeout: float = 30,
    attempts: int = 1,
) -> OracleStep:
    return shell(
        node,
        f"kubectl {args} 2>&1 | head -60",
        purpose,
        expect,
        evidence=evidence,
        timeout=timeout,
        attempts=attempts,
    )


def p4rt(
    args: str,
    purpose: str,
    expect: Check | None = None,
    *,
    evidence: bool = False,
    attempts: int = 1,
    interval_sec: float = 5.0,
) -> OracleStep:
    """Read-only P4Runtime query through the fabric controller (``p4rt_exec``)."""
    return OracleStep(
        purpose,
        "p4rt_exec",
        {"args": args},
        expect,
        evidence=evidence,
        attempts=attempts,
        interval_sec=interval_sec,
    )


def onos(
    path: str, purpose: str, expect: Check | None = None, *, evidence: bool = False
) -> OracleStep:
    """ONOS REST query through the fabric controller."""
    return OracleStep(
        purpose, "sdn_onos_rest", {"path": path}, expect, evidence=evidence
    )


def http_probe(
    node: str,
    url: str,
    purpose: str,
    expect: Check | None = None,
    *,
    evidence: bool = False,
    times: int = 3,
) -> OracleStep:
    return OracleStep(
        purpose,
        "curl_web_test",
        {"host_name": node, "url": url, "times": times},
        expect,
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Probes toward an address read in an earlier step
# ---------------------------------------------------------------------------

_IPV4_PREFIX = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})/\d+")


def is_ip_node(net_env, node: str) -> bool:
    """Whether ``node`` is a Linux host or router that holds interface addresses."""
    identity = (getattr(net_env, "machine_identities", None) or {}).get(node)
    if identity is None:
        return False
    caps = set(identity.capabilities)
    role = getattr(identity.role, "value", identity.role)
    return "linux" in caps and role != "switch" and not caps & {"srl", "ovs"}


def addr_step(node: str, intf: str, key: str) -> OracleStep:
    """Read the IPv4 address of ``node:intf`` for later probes (saved as ``key``)."""
    return shell(
        node,
        f"ip -4 -br addr show dev {intf}",
        f"Read the IPv4 address of {node}:{intf} for the probes that follow.",
        contains(r"\d+\.\d+\.\d+\.\d+/\d+", f"{node}:{intf} has an IPv4 address"),
        save_as=key,
    )


def toward(
    src: str,
    key: str,
    command: str,
    purpose: str,
    expect: Check | None,
    *,
    evidence: bool = False,
    timeout: float = 30,
) -> OracleStep:
    """Run ``command`` on ``src`` with ``{ip}`` set to the address saved under ``key``.

    The step fails when the earlier step read no address.
    """

    def args(outputs: Mapping[str, str]) -> dict[str, Any]:
        match = _IPV4_PREFIX.search(outputs.get(key, ""))
        if match is None:
            raise ValueError(f"no IPv4 address was read for {key!r}")
        return {
            "host_name": src,
            "command": command.replace("{ip}", match.group(1)),
            "timeout": timeout,
        }

    return OracleStep(purpose, "exec_shell", args, expect, evidence=evidence)


def _pingmesh_clean(output: str) -> bool:
    try:
        summary = json.loads(output).get("summary") or {}
    except (ValueError, AttributeError):
        return False
    return summary.get("anomaly_count") == 0 and summary.get("total_pairs", 0) > 0


def healthy_playbook() -> OraclePlaybook:
    """No-fault control: the mesh is clean, so no root cause is reported."""
    return OraclePlaybook(
        finding="No fault: every probed endpoint pair is reachable within its latency budget.",
        steps=[
            OracleStep(
                "Measure reachability and latency across the endpoint mesh.",
                "run_pingmesh_snapshot",
                {},
                Check(
                    "the snapshot probes endpoint pairs and reports no anomalies",
                    _pingmesh_clean,
                ),
                evidence=True,
                attempts=2,
                interval_sec=20,
            )
        ],
        anomaly=False,
    )
