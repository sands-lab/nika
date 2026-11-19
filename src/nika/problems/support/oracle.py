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


def service_address_command(net_env: Any, host: str) -> str:
    """Read the address on the host's topology data interface."""
    from nika.problems.rca.inventory import kernel_data_interface

    interface = kernel_data_interface(net_env, host)
    return f"ip -4 -o addr show dev {shlex.quote(interface)} scope global"


def is_service_ipv4(value: str) -> bool:
    """Whether an IPv4 address can represent a lab service endpoint."""
    import ipaddress
    from nika.telemetry import TELEMETRY_SUBNET

    try:
        address = ipaddress.ip_interface(value).ip
    except ValueError:
        return False
    return (
        address.version == 4
        and not address.is_loopback
        and address not in ipaddress.ip_network(TELEMETRY_SUBNET)
    )


def containerlab_data_eth0(runtime: Any) -> set[str] | None:
    """Return nodes with a declared eth0 data link, or None for other backends."""
    if getattr(runtime, "backend", None) != "containerlab":
        return None
    topology = getattr(runtime, "topology_file", None)
    if not topology:
        return set()
    from nika.runtime.containerlab.parse import parse_clab_topology

    return {
        node
        for link in parse_clab_topology(topology).links
        for endpoint in link.endpoints
        for node, interface in [endpoint.split(":", 1)]
        if interface == "eth0"
    }


def management_interfaces(data_eth0: set[str] | None, node: str) -> set[str]:
    """Identify Containerlab's management interface using the data topology."""
    if data_eth0 is None:
        return set()
    return {"mgmt0", "br-mgmt"} | (set() if node in data_eth0 else {"eth0"})


def management_networks(runtime: Any) -> tuple:
    """Return Docker bridge networks used by Kathara bridged nodes."""
    if getattr(runtime, "backend", None) != "kathara":
        return ()
    from nika.service.lab.semantic_mixin import _docker_bridge_networks

    return _docker_bridge_networks()


def service_addresses(
    output: str,
    interface: str | None = None,
    *,
    excluded_interfaces: frozenset[str] | set[str] = frozenset(),
    excluded_networks: tuple = (),
) -> list[tuple[str, str]]:
    """Return observed data-plane interface and IPv4 CIDR pairs."""
    import ipaddress

    addresses = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] != "inet":
            continue
        name = parts[1].split("@", 1)[0]
        if name in excluded_interfaces or (interface is not None and name != interface):
            continue
        if is_service_ipv4(parts[3]) and not any(
            ipaddress.ip_interface(parts[3]).ip in network
            for network in excluded_networks
        ):
            addresses.append((name, parts[3]))
    return addresses


def service_address(output: str, interface: str) -> str:
    """Select an address on the topology interface from a live observation."""
    import ipaddress

    addresses = [
        ipaddress.ip_interface(cidr).ip
        for _, cidr in service_addresses(output, interface)
    ]
    if not addresses:
        raise ValueError(f"No service address observed on {interface}.")
    return str(min(addresses))


@dataclass(frozen=True)
class Check:
    """Expected observation in one tool result."""

    description: str
    predicate: Callable[[str], bool]
    observations: Callable[[str, Mapping[str, str]], bool] | None = None

    def __call__(self, output: str, outputs: Mapping[str, str] | None = None) -> bool:
        if self.observations is not None:
            return bool(self.observations(output, outputs or {}))
        return bool(self.predicate(output))


def compare(
    description: str, predicate: Callable[[str, Mapping[str, str]], bool]
) -> Check:
    """Compare a device observation with earlier saved observations in Python."""
    return Check(description, lambda _: False, observations=predicate)


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


def any_of(*checks: Check) -> Check:
    return Check(
        " or ".join(c.description for c in checks),
        lambda o: any(c(o) for c in checks),
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


# ---------------------------------------------------------------------------
# Step templates
# ---------------------------------------------------------------------------


def triage() -> OracleStep:
    """Measure the user-visible symptom across the endpoint mesh."""
    return OracleStep(
        "Triage: measure reachability and latency across the endpoint mesh.",
        "prometheus_query",
        {"query": "min by (source,module,expected) (avg_over_time(probe_success[2m]))"},
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


def iosxr(
    router: str,
    command: str,
    purpose: str,
    expect: Check | None = None,
    *,
    evidence: bool = False,
    attempts: int = 1,
    interval_sec: float = 5.0,
    save_as: str | None = None,
) -> OracleStep:
    """Run an IOS XR CLI *command* on *router* with ``iosxr_exec``."""
    return OracleStep(
        purpose,
        "iosxr_exec",
        {"router_name": router, "command": command},
        expect,
        evidence=evidence,
        attempts=attempts,
        interval_sec=interval_sec,
        save_as=save_as,
    )


def routeros(
    router: str,
    command: str,
    purpose: str,
    expect: Check | None = None,
    *,
    evidence: bool = False,
    attempts: int = 1,
    interval_sec: float = 5.0,
    save_as: str | None = None,
) -> OracleStep:
    """Run a RouterOS CLI *command* on *router* with ``routeros_exec``."""
    return OracleStep(
        purpose,
        "routeros_exec",
        {"router_name": router, "command": command},
        expect,
        evidence=evidence,
        attempts=attempts,
        interval_sec=interval_sec,
        save_as=save_as,
    )


def srl(
    router: str,
    command: str,
    purpose: str,
    expect: Check | None = None,
    *,
    evidence: bool = False,
    attempts: int = 1,
    interval_sec: float = 5.0,
    save_as: str | None = None,
) -> OracleStep:
    """Run an SR Linux CLI command through the agent's ``srl_exec_cli`` tool."""
    return OracleStep(
        purpose,
        "srl_exec_cli",
        {"device_name": router, "command": command},
        expect,
        evidence=evidence,
        attempts=attempts,
        interval_sec=interval_sec,
        save_as=save_as,
    )


def router_cli(
    net_env,
    router: str,
    purpose: str,
    expect: Check | None = None,
    *,
    frr: str,
    iosxr_command: str | None = None,
    routeros_command: str | None = None,
    srl_command: str | None = None,
    sonic_command: str | None = None,
    evidence: bool = False,
    attempts: int = 1,
    interval_sec: float = 5.0,
    save_as: str | None = None,
) -> OracleStep:
    """Read *router* with the CLI of its platform.

    FRR routers run *frr* through ``exec_shell``. IOS XR, RouterOS, and SR Linux
    run the matching command through their MCP tool. A missing platform command
    raises ``ValueError``.
    """
    from nika.problems.support.router_ops import native_router_profile

    profile = native_router_profile(net_env, router)
    options = dict(
        evidence=evidence,
        attempts=attempts,
        interval_sec=interval_sec,
        save_as=save_as,
    )
    if srl_cli(net_env, router):
        if srl_command is None:
            raise ValueError(f"No SR Linux command for {purpose!r}")
        return srl(router, srl_command, purpose, expect, **options)
    if profile == "sonic" and sonic_command is not None:
        return OracleStep(
            purpose,
            "sonic_exec_cli",
            {"device_name": router, "command": sonic_command},
            expect,
            **options,
        )
    if profile == "iosxr":
        if iosxr_command is None:
            raise ValueError(f"No IOS XR command for {purpose!r}")
        return iosxr(router, iosxr_command, purpose, expect, **options)
    if profile == "routeros":
        if routeros_command is None:
            raise ValueError(f"No RouterOS command for {purpose!r}")
        return routeros(router, routeros_command, purpose, expect, **options)
    return shell(router, frr, purpose, expect, **options)


def interface_state(
    net_env, node: str, intf: str, purpose: str, expect: Check | None = None, **options
) -> OracleStep:
    """Read physical carrier separately from a virtual NOS's protocol state."""
    from nika.runtime.spec import NodeRole

    identity = net_env.machine_identities.get(node)
    if identity is not None and identity.role in {NodeRole.ROUTER, NodeRole.SWITCH}:
        return OracleStep(
            purpose,
            "device_interface_state",
            {"host_name": node, "interface": intf},
            expect,
            **options,
        )
    return interface_read(net_env, node, intf, purpose, expect, **options)


def interface_read(
    net_env, node: str, intf: str, purpose: str, expect: Check | None = None, **options
) -> OracleStep:
    """Read interface operational state through the node's declared CLI."""
    from nika.net_env.utils.iosxr.common import iosxr_interface
    from nika.net_env.utils.routeros.common import routeros_port
    from nika.service.containerlab.srl_api import srl_subif
    from nika.net_env.utils.sonic.common import sonic_port
    from nika.problems.support.router_ops import native_router_profile

    identity = net_env.machine_identities.get(node)
    caps = set(identity.capabilities) if identity else set()
    if "bmv2" in caps:
        return OracleStep(
            purpose,
            "bmv2_get_ports",
            {"switch": node, "interface": intf},
            expect,
            **options,
        )
    if "ovs" in caps:
        return shell(
            node, f"ovs-vsctl list interface {intf}", purpose, expect, **options
        )
    profile = native_router_profile(net_env, node)
    return router_cli(
        net_env,
        node,
        purpose,
        expect,
        frr=f"ip -d -s link show dev {intf}",
        iosxr_command=f"show ipv4 interface {iosxr_interface(intf)}"
        if profile == "iosxr"
        else None,
        routeros_command=f":put [/interface get [find name={routeros_port(net_env, node, intf)}] running]; /interface print detail where name={routeros_port(net_env, node, intf)}"
        if profile == "routeros"
        else None,
        srl_command=f"info from state interface {srl_subif(intf).split('.')[0]}",
        sonic_command=f"show interfaces status {sonic_port(intf) if intf.startswith('eth') else intf}"
        if profile == "sonic"
        else None,
        **options,
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


# nftables ruleset structure and drop rules, then iptables drop rules, so the
# same step reads a node that filters with either tool.
FILTER_DUMP = (
    "nft list ruleset 2>/dev/null | grep -nE 'table|chain|hook|drop|reject' "
    "| head -60; iptables -S 2>/dev/null | grep -E -- '-j (DROP|REJECT)' | head -40"
)


def iptables_rule(rule: str, description: str) -> Check:
    """An ``iptables -S`` drop rule matches *rule* (regex) before its target."""
    return contains(
        rf"^-A \S+ .*{rule}.* -j (DROP|REJECT)", f"{description} (iptables)"
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
        f"out=$(pgrep -a {shlex.quote(pattern)}); rc=$?; "
        "if [ $rc -eq 0 ]; then printf '%s\\n' \"$out\" | "
        "awk '!/ <defunct>$/ {print; found=1} END {if (!found) print \"NO_PROCESS\"}'; "
        "elif [ $rc -eq 1 ]; then echo NO_PROCESS; else echo NO_PGREP; fi"
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


def srl_cli(net_env, node: str) -> bool:
    """Whether ``node`` is an SR Linux router, read through ``srl_exec_cli``.

    Other routers, SONiC included, answer FRR and Linux commands in
    ``exec_shell``.
    """
    identity = (getattr(net_env, "machine_identities", None) or {}).get(node)
    return identity is not None and bool(
        {"srl", "nokia_srlinux"} & set(identity.capabilities)
    )


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
    attempts: int = 1,
    interval_sec: float = 5.0,
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

    return OracleStep(
        purpose,
        "exec_shell",
        args,
        expect,
        evidence=evidence,
        attempts=attempts,
        interval_sec=interval_sec,
    )


def _pingmesh_clean(output: str) -> bool:
    try:
        rows = json.loads(output)
        rows = [rows] if isinstance(rows, dict) else rows
        return (
            isinstance(rows, list)
            and bool(rows)
            and all(row["value"] == 0 for row in rows)
        )
    except (ValueError, KeyError, TypeError):
        return False


def healthy_playbook() -> OraclePlaybook:
    """No-fault control: the mesh is clean, so no root cause is reported."""
    return OraclePlaybook(
        finding="No fault: recorded endpoint probes match the environment's reachability policy.",
        steps=[
            OracleStep(
                "Measure reachability and latency across the endpoint mesh.",
                "prometheus_query",
                {
                    "query": 'sum(min_over_time(probe_success{expected="reachable"}[1m]) < bool 1) + (sum(max_over_time(probe_success{expected="isolated"}[1m])) or vector(0))'
                },
                Check(
                    "recorded probes reach expected destinations and preserve declared isolation",
                    _pingmesh_clean,
                ),
                evidence=True,
                attempts=2,
                interval_sec=20,
            )
        ],
        anomaly=False,
    )
