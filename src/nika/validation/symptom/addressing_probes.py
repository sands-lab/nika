"""Host addressing symptom probes (test-path only).

The baseline chooses the probe endpoints on the healthy lab and keeps them on
the problem, so the sham, symptom, and later reads measure the same path. A
misconfigured host is observed from a peer, never from the host itself.
"""

from __future__ import annotations

import ipaddress
import json
import re
import shlex
import time
from collections.abc import Callable
from typing import Any, TypeVar

from nika.net_env.verify import exec_or_empty, ping_stats
from nika.problems.base import build_verify_result
from nika.runtime.base import LabRuntime
from nika.runtime.spec import NodeRole

_HOST_PREFIXES = ("client", "pc", "web", "service", "host", "server")
_ECHO_COUNT = 10
# Healthy-baseline reads: a healthy path may shed an occasional echo (the
# enterprise WAN loses about 1 in 3000 under host scheduling noise), so a
# lossy read is repeated before the path is called unhealthy. A freshly
# deployed lab under host load can also lose a path for about a minute while
# routing settles, so the reads span roughly 70 s.
HEALTHY_READS = 6
HEALTHY_READ_GAP_SEC = 5.0
_T = TypeVar("_T")
_ARP_COUNT = 4
_FLAP_ECHOES = 50
# While the bridge points the shared MAC at one port, echoes to both holders
# reach that host and only its own are answered: about half of the pair's
# echoes are lost, and each holder loses some. A healthy LAN loses none.
_MIN_PAIR_LOSS_PERCENT = 25.0
_MIN_HOLDER_LOSS_PERCENT = 4.0


def _valid_ipv4(text: str) -> str:
    try:
        parsed = ipaddress.ip_address(text)
    except ValueError:
        return ""
    return text if parsed.version == 4 else ""


def iface_ipv4(runtime: LabRuntime, node: str, iface: str) -> str:
    """IPv4 address configured on ``iface``, or empty if that port has none."""
    quoted = shlex.quote(iface)
    out = exec_or_empty(
        runtime,
        node,
        f"ip -4 -o addr show dev {quoted} scope global | awk '{{print $4}}'",
    )
    cidr = next((line.strip() for line in out.splitlines() if line.strip()), "")
    return _valid_ipv4(cidr.split("/")[0])


def _eth0_ip(runtime: LabRuntime, node: str) -> str:
    return iface_ipv4(runtime, node, "eth0")


_MGMT_PREFIXES = ("127.", "172.100.", "172.101.", "172.20.", "172.18.")
_MGMT_IFACES = {"mgmt0", "eth0"}


def _probe_ips(runtime: LabRuntime, node: str) -> list[str]:
    """Data-plane IPv4 addresses on ``node``, loopbacks included, management omitted.

    Kathara hosts keep the pingable address on eth0. Containerlab linux nodes
    use eth1, and SR Linux / SONiC routers use platform ports plus a routed
    loopback; eth0/mgmt0 are the Containerlab management network.
    """
    found: list[str] = []
    seen: set[str] = set()

    def add(ip: str) -> None:
        ip = _valid_ipv4(ip)
        if ip and ip not in seen and not ip.startswith(_MGMT_PREFIXES):
            seen.add(ip)
            found.append(ip)

    getter = getattr(runtime, "get_data_plane_host_ip", None)
    if callable(getter):
        add(getter(node, with_prefix=False) or "")
    output = exec_or_empty(runtime, node, "ip -j -4 addr")
    try:
        links = json.loads(output) if output.lstrip().startswith("[") else []
    except json.JSONDecodeError:
        links = []
    if not isinstance(links, list):
        links = []
    for link in links:
        if not isinstance(link, dict):
            continue
        name = str(link.get("ifname") or "").split("@", 1)[0]
        if name in _MGMT_IFACES:
            continue
        for addr in link.get("addr_info") or []:
            if not isinstance(addr, dict) or addr.get("family") not in ("inet", 2):
                continue
            add(str(addr.get("local") or ""))
    add(_eth0_ip(runtime, node))
    return found


def lab_addresses(runtime: LabRuntime) -> dict[str, list[tuple[str, str]]]:
    """Global IPv4 ``(interface, address/prefix)`` pairs of every lab node."""
    found: dict[str, list[tuple[str, str]]] = {}
    for node in sorted(runtime.list_nodes()):
        output = exec_or_empty(runtime, node, "ip -4 -o addr show scope global")
        found[node] = re.findall(r"^\d+:\s+(\S+)\s+inet\s+(\S+)", output, re.M)
    return found


def healthy_read(
    read: Callable[[], _T], loss_percent: Callable[[_T], float]
) -> tuple[_T, int]:
    """Repeat a healthy-baseline read until it has no loss; return it and the read count.

    A path that stays lossy fails every read, so only transient loss is absorbed.
    Fault-time predicates must not use this.
    """
    for attempt in range(HEALTHY_READS):
        if attempt:
            time.sleep(HEALTHY_READ_GAP_SEC)
        result = read()
        if loss_percent(result) == 0.0:
            break
    return result, attempt + 1


def _reachable(runtime: LabRuntime, src: str, ip: str) -> bool:
    """The address is live and routed from ``src``: at least one of 3 echoes answers.

    Whether the path is clean is the baseline's read, which retries lossy reads.
    """
    return ping_stats(runtime, src, ip, count=3, interval_sec=0.2).loss_percent < 100.0


def _observers(runtime: LabRuntime, target_ip: str, exclude: set[str]) -> dict:
    """Pick a host that reaches ``target_ip`` and one more host as control.

    Labs whose only hosts are the faulted pair (llmd_lab) observe from any
    other node on the segment.
    """
    nodes = [node for node in sorted(runtime.list_nodes()) if node not in exclude]
    candidates = [node for node in nodes if node.startswith(_HOST_PREFIXES)] or nodes
    observer = next(
        (node for node in candidates[:12] if _reachable(runtime, node, target_ip)),
        None,
    )
    if observer is None:
        return {}
    for node in candidates + [node for node in nodes if node not in candidates]:
        if node == observer:
            continue
        for ip in _probe_ips(runtime, node):
            if ip != target_ip and _reachable(runtime, observer, ip):
                return {"observer": observer, "control_ip": ip, "target_ip": target_ip}
    # SR Linux / SONiC keep IPv4 in the NOS, not in the kernel, so other
    # routers have no Linux address to ping. The observer's default gateway
    # is still a data-plane address it can reach.
    getter = getattr(runtime, "get_default_gateway", None)
    gateway = _valid_ipv4(getter(observer) or "") if callable(getter) else ""
    if gateway and gateway != target_ip and _reachable(runtime, observer, gateway):
        return {
            "observer": observer,
            "control_ip": gateway,
            "target_ip": target_ip,
        }
    return {}


def _loss(runtime: LabRuntime, src: str, ip: str) -> float:
    return ping_stats(
        runtime, src, ip, count=_ECHO_COUNT, interval_sec=0.2
    ).loss_percent


def _healthy_loss(runtime: LabRuntime, src: str, ip: str) -> float:
    loss, _reads = healthy_read(lambda: _loss(runtime, src, ip), lambda value: value)
    return loss


def address_baseline(
    problem: Any, host: str, intf: str = "eth0"
) -> tuple[bool, dict[str, Any]]:
    """A peer reaches ``host`` at its assigned address, and a control host."""
    runtime = problem.runtime
    target_ip = (runtime.get_host_ip(host, intf, with_prefix=True) or "").split("/")[0]
    ends = _observers(runtime, target_ip, {host}) if target_ip else {}
    if not ends:
        return False, {"error": "no_peer_reaches_target", "target_host": host}
    ends = {**ends, "target_host": host}
    problem._audit_address_ends = ends
    target_loss = _healthy_loss(runtime, ends["observer"], ends["target_ip"])
    control_loss = _healthy_loss(runtime, ends["observer"], ends["control_ip"])
    ok = target_loss == 0.0 and control_loss == 0.0
    return ok, {
        **ends,
        "target_loss_percent": target_loss,
        "control_loss_percent": control_loss,
    }


def address_unreachable(problem: Any, label: str) -> tuple[bool, dict[str, Any]]:
    """Echoes from the peer to the assigned address are lost; the control's are not."""
    ends = getattr(problem, "_audit_address_ends", None) or {}
    if not ends:
        return False, {"error": "no_baseline_observer"}
    runtime = problem.runtime
    target_loss = _loss(runtime, ends["observer"], ends["target_ip"])
    control_loss = _loss(runtime, ends["observer"], ends["control_ip"])
    control_ok = control_loss == 0.0
    verified = target_loss == 100.0 and control_ok
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            **ends,
            "target": label,
            "echoes": _ECHO_COUNT,
            "target_loss_percent": target_loss,
            "control_loss_percent": control_loss,
            "control_ok": control_ok,
        },
    )


def assigned_address_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    return address_baseline(
        problem, params.host_name, getattr(params, "intf_name", "eth0")
    )


def assigned_address_unreachable(
    problem: Any, params: Any
) -> tuple[bool, dict[str, Any]]:
    """Peers can no longer reach the host at the address it should hold."""
    return address_unreachable(problem, "assigned_address")


def _mac_conflict_ends(problem: Any, params: Any) -> dict[str, Any]:
    """Both holders of the MAC, a third host on their segment, and a control.

    The observer and the control are other nodes on the owner's subnet (end
    hosts first), so the echoes cross the same learning bridge as the pair.
    """
    runtime = problem.runtime
    claimant, owner = params.host_name, params.host_name_2
    cidr = runtime.get_host_ip(owner, "eth0", with_prefix=True) or ""
    if not cidr:
        return {}
    network = ipaddress.ip_interface(cidr).network
    claimant_ip = _eth0_ip(runtime, claimant)
    if not claimant_ip or ipaddress.ip_address(claimant_ip) not in network:
        return {"error": "pair_not_on_one_segment", "network": str(network)}
    roles = problem.net_env.machine_identities
    members = sorted(
        (
            getattr(roles.get(node), "role", None)
            not in {NodeRole.HOST, NodeRole.SERVICE},
            node,
            str(ipaddress.ip_interface(address).ip),
        )
        for node, rows in lab_addresses(runtime).items()
        if node not in {claimant, owner}
        for _intf, address in rows
        if ipaddress.ip_interface(address).ip in network
    )
    if len(members) < 2:
        return {"error": "no_third_node_on_the_segment", "network": str(network)}
    (_r, observer, _ip), (_c, control, control_ip) = members[:2]
    return {
        "observer": observer,
        "owner": owner,
        "owner_ip": cidr.split("/")[0],
        "claimant": claimant,
        "claimant_ip": claimant_ip,
        "control": control,
        "control_ip": control_ip,
    }


def _mac_conflict_reading(problem: Any, ends: dict[str, Any]) -> dict[str, Any]:
    """Concurrent echoes from the observer to the owner, the claimant and the control.

    The observer first drops its neighbour entries for the three addresses and
    resolves them again, as a fresh client would, so the reading measures the
    shared MAC rather than a stale ARP entry for the claimant's old MAC.
    """
    targets = {role: ends[f"{role}_ip"] for role in ("owner", "claimant", "control")}
    flush = "; ".join(f"ip neigh flush to {ip}" for ip in targets.values())
    pings = " ".join(
        f"(echo {ip} $(ping -c {_FLAP_ECHOES} -i 0.2 -W 1 -q {ip}"
        " | grep -o '[0-9]* received')) &"
        for ip in targets.values()
    )
    output = exec_or_empty(
        problem.runtime,
        ends["observer"],
        f"{flush}; {pings} wait",
        timeout=_FLAP_ECHOES * 0.2 + 20,
    )
    received = dict(re.findall(r"^(\S+) (\d+) received", output, re.M))
    return {
        f"{role}_loss_percent": (
            100.0 * (1 - int(received[ip]) / _FLAP_ECHOES) if ip in received else None
        )
        for role, ip in targets.items()
    }


def mac_conflict_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """On the healthy segment the observer reaches the pair and the control."""
    ends = _mac_conflict_ends(problem, params)
    problem._audit_mac_ends = ends
    if not ends or "error" in ends:
        return False, ends or {"error": "owner_has_no_address"}
    reading, _reads = healthy_read(
        lambda: _mac_conflict_reading(problem, ends),
        lambda read: max((1e9 if v is None else v) for v in read.values()),
    )
    ok = all(loss == 0.0 for loss in reading.values())
    return ok, {**ends, "echoes": _FLAP_ECHOES, **reading}


def mac_conflict(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Traffic to both holders of the MAC is lost; the control's is not.

    The bridge learns the shared MAC on whichever port sent last, so frames
    for one host are delivered to the other while both answer traffic. A
    wrong or stale MAC loses only one host's echoes; a conflict loses both.
    """
    ends = getattr(problem, "_audit_mac_ends", None) or {}
    if not ends or "error" in ends:
        return False, {"error": "no_baseline_observer"}
    reading = _mac_conflict_reading(problem, ends)
    control_ok = reading["control_loss_percent"] == 0.0
    holders = [reading[f"{role}_loss_percent"] for role in ("owner", "claimant")]
    measured = None not in holders
    pair_loss = sum(holders) / 2 if measured else None
    verified = (
        control_ok
        and measured
        and pair_loss >= _MIN_PAIR_LOSS_PERCENT
        and min(holders) >= _MIN_HOLDER_LOSS_PERCENT
    )
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            **ends,
            "echoes": _FLAP_ECHOES,
            **reading,
            "pair_loss_percent": pair_loss,
            "min_pair_loss_percent": _MIN_PAIR_LOSS_PERCENT,
            "min_holder_loss_percent": _MIN_HOLDER_LOSS_PERCENT,
            "control_ok": control_ok,
        },
    )


def _netmask_ends(problem: Any, params: Any) -> dict[str, Any]:
    """A host inside the widened prefix but outside the real subnet, and a sibling.

    With the wider mask, the misconfigured host treats that remote host as
    on-link, so its replies go to an address that never answers ARP. A
    sibling inside the real subnet stays on-link either way and is the control.
    """
    runtime = problem.runtime
    host = params.host_name
    real = ipaddress.ip_interface(
        runtime.get_host_ip(host, "eth0", with_prefix=True) or "0.0.0.0/32"
    )
    wide = ipaddress.ip_interface(f"{real.ip}/{params.netmask_prefix}").network
    remote: list[str] = []
    sibling: list[str] = []
    for node, rows in lab_addresses(runtime).items():
        ips = [ipaddress.ip_interface(address).ip for _intf, address in rows]
        if node == host:
            continue
        if any(ip in real.network for ip in ips):
            sibling.append(node)
        elif any(ip in wide for ip in ips):
            # Nodes with an address in the real subnet (the gateway) stay on-link.
            remote.append(node)

    def end_hosts_first(nodes: list[str]) -> list[str]:
        roles = problem.net_env.machine_identities
        return sorted(
            nodes,
            key=lambda node: (
                getattr(roles.get(node), "role", None)
                not in {NodeRole.HOST, NodeRole.SERVICE}
            ),
        )

    target_ip = str(real.ip)
    observer = next(
        (n for n in end_hosts_first(remote) if _reachable(runtime, n, target_ip)),
        None,
    )
    control = next(
        (n for n in end_hosts_first(sibling) if _reachable(runtime, n, target_ip)),
        None,
    )
    return {
        "target_host": host,
        "target_ip": target_ip,
        "real_network": str(real.network),
        "wide_network": str(wide),
        "observer": observer,
        "control_observer": control,
    }


def netmask_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    ends = _netmask_ends(problem, params)
    problem._audit_netmask_ends = ends
    if not ends["observer"]:
        return False, {**ends, "error": "no_host_in_the_widened_prefix"}
    if not ends["control_observer"]:
        return False, {**ends, "error": "no_sibling_in_the_real_subnet"}
    runtime = problem.runtime
    target_loss = _healthy_loss(runtime, ends["observer"], ends["target_ip"])
    control_loss = _healthy_loss(runtime, ends["control_observer"], ends["target_ip"])
    ok = target_loss == 0.0 and control_loss == 0.0
    return ok, {
        **ends,
        "target_loss_percent": target_loss,
        "control_loss_percent": control_loss,
    }


def netmask_offsubnet_unreachable(
    problem: Any, params: Any
) -> tuple[bool, dict[str, Any]]:
    """A remote host inside the wrong prefix loses the host; a sibling does not."""
    ends = getattr(problem, "_audit_netmask_ends", None) or {}
    if not ends.get("observer") or not ends.get("control_observer"):
        return False, {"error": "no_baseline_observer"}
    runtime = problem.runtime
    target_loss = _loss(runtime, ends["observer"], ends["target_ip"])
    control_loss = _loss(runtime, ends["control_observer"], ends["target_ip"])
    control_ok = control_loss == 0.0
    verified = target_loss == 100.0 and control_ok
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            **ends,
            "echoes": _ECHO_COUNT,
            "target_loss_percent": target_loss,
            "control_loss_percent": control_loss,
            "control_ok": control_ok,
        },
    )


def _arp_responders(
    runtime: LabRuntime, observer: str, intf: str, ip: str
) -> tuple[list[str], str]:
    """MAC addresses that answer broadcast ARP requests for ``ip``.

    Images ship iputils or BusyBox arping, which need ``-b`` to keep
    broadcasting after the first reply, or Habets arping, which always
    broadcasts and needs ``-0`` for a 0.0.0.0 sender. Habets also prints
    replies differently.
    """
    # Habets reads -w as the gap between requests in microseconds, not a deadline.
    target = f"-c {_ARP_COUNT} -I {intf} {ip}"
    output = exec_or_empty(
        runtime,
        observer,
        f"if arping --help 2>&1 | grep -qi habets; then arping -0 {target}; "
        f"else arping -b -w {_ARP_COUNT + 2} {target}; fi",
        timeout=_ARP_COUNT + 10,
    )
    macs = re.findall(r"reply from \S+ \[([0-9A-Fa-f:]{17})\]", output)
    macs += re.findall(r"bytes from ([0-9A-Fa-f:]{17}) \(", output)
    return sorted({mac.lower() for mac in macs}), output[-600:]


def _arp_ends(problem: Any, params: Any) -> dict[str, Any]:
    """A third node on the segment of the copied address, and a control address.

    The control is another node's address on the same segment; exactly one
    host answers ARP for it before and after inject.
    """
    runtime = problem.runtime
    cidr = runtime.get_host_ip(params.host_name, "eth0", with_prefix=True) or ""
    if not cidr:
        return {}
    network = ipaddress.ip_interface(cidr).network
    pair = {params.host_name, params.host_name_2}
    members = [
        (node, intf, str(ipaddress.ip_interface(address).ip))
        for node, rows in lab_addresses(runtime).items()
        if node not in pair
        for intf, address in rows
        if ipaddress.ip_interface(address).ip in network
    ]
    observer, intf, _ip = members[0] if members else ("", "", "")
    control = next((ip for node, _i, ip in members if node != observer), None)
    if not control:
        return {}
    return {
        "observer": observer,
        "observer_intf": intf,
        "address": cidr.split("/")[0],
        "control_address": control,
        "owner_mac": runtime.get_host_mac_address(params.host_name, "eth0").lower(),
        "claimant_mac": runtime.get_host_mac_address(
            params.host_name_2, "eth0"
        ).lower(),
    }


def _arp_reading(problem: Any, ends: dict[str, Any]) -> dict[str, Any]:
    runtime = problem.runtime
    macs, raw = _arp_responders(
        runtime, ends["observer"], ends["observer_intf"], ends["address"]
    )
    control, _raw = _arp_responders(
        runtime, ends["observer"], ends["observer_intf"], ends["control_address"]
    )
    return {"arp_responders": macs, "control_responders": control, "raw": raw}


def ip_conflict_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Only the owner answers ARP for its address on the healthy segment."""
    ends = _arp_ends(problem, params)
    problem._audit_arp_ends = ends
    if not ends:
        return False, {"error": "no_third_node_on_the_segment"}
    reading = _arp_reading(problem, ends)
    ok = reading["arp_responders"] == [ends["owner_mac"]]
    return ok and len(reading["control_responders"]) == 1, {**ends, **reading}


def ip_conflict(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Two hosts answer ARP for the same address, as a third node sees it."""
    ends = getattr(problem, "_audit_arp_ends", None) or {}
    if not ends:
        return False, {"error": "no_baseline_observer"}
    reading = _arp_reading(problem, ends)
    control_ok = len(reading["control_responders"]) == 1
    verified = (
        ends["owner_mac"] in reading["arp_responders"]
        and ends["claimant_mac"] in reading["arp_responders"]
        and control_ok
    )
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={**ends, **reading, "control_ok": control_ok},
    )
