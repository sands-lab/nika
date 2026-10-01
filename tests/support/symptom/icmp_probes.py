"""ICMP Fragmentation Needed filtering probe (test-path only).

A host behind the filtering node receives ICMP Destination Unreachable
messages sent across that node. Code 4 (Fragmentation Needed) is the probe
signal; code 3 (Port Unreachable) on the same path is the control.
"""

from __future__ import annotations

import shlex
from typing import Any

from nika.net_env.verify import ping_stats
from nika.problems.base import build_verify_result
from nika.runtime.base import LabRuntime

_HOST_PREFIXES = ("client", "pc", "web", "service", "host", "server")
_COUNT = 5

# Sends ``count`` ICMP type 3 messages with ``code`` that quote a TCP packet
# from the receiver, as a router on a smaller-MTU hop would.
_SENDER = r"""
import socket, struct, sys
dst, code, count = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
def checksum(data):
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    total = (total >> 16) + (total & 0xFFFF)
    return ~(total + (total >> 16)) & 0xFFFF
src = socket.inet_aton(sys.argv[4])
inner = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 1500, 1, 0x4000, 64, 6, 0,
                    socket.inet_aton(dst), src) + struct.pack("!HHI", 40000, 80, 1)
sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
for _ in range(count):
    head = struct.pack("!BBHHH", 3, code, 0, 0, 1280)
    packet = head + inner
    packet = packet[:2] + struct.pack("!H", checksum(packet)) + packet[4:]
    sock.sendto(packet, (dst, 0))
"""


def _eth0_ip(runtime: LabRuntime, node: str) -> str:
    out = runtime.exec(
        node, "ip -4 -o addr show dev eth0 scope global | awk '{print $4}'"
    )
    return out.strip().split("/")[0]


def _endpoints(runtime: LabRuntime, node: str) -> dict[str, str]:
    """Pick a host attached to ``node`` and a remote host that reaches it."""
    neighbors = runtime.get_connected_devices(node)
    receiver = next((n for n in neighbors if n.startswith(_HOST_PREFIXES)), None)
    if receiver is None:
        raise RuntimeError(f"no end host is attached to {node}")
    receiver_ip = _eth0_ip(runtime, receiver)
    for sender in sorted(runtime.list_nodes()):
        if (
            sender in neighbors
            or sender == node
            or not sender.startswith(_HOST_PREFIXES)
        ):
            continue
        stats = ping_stats(runtime, sender, receiver_ip, count=2, interval_sec=0.2)
        if stats.loss_percent == 0.0:
            return {
                "sender": sender,
                "sender_ip": _eth0_ip(runtime, sender),
                "receiver": receiver,
                "receiver_ip": receiver_ip,
            }
    raise RuntimeError(f"no remote host reaches {receiver} across {node}")


def _delivered(runtime: LabRuntime, ends: dict[str, str]) -> dict[str, int]:
    """Count code 4 and code 3 messages that reach the receiver."""
    capture = "/tmp/nika-audit-icmp.pcap"
    runtime.exec(
        ends["receiver"],
        f"rm -f {capture}; (timeout 8 tcpdump -ni eth0 -w {capture} "
        "'icmp[0] == 3' >/dev/null 2>&1 &)",
    )
    runtime.exec(ends["receiver"], "sleep 1")
    for code in (4, 3):
        runtime.exec(
            ends["sender"],
            f"python3 -c {shlex.quote(_SENDER)} {ends['receiver_ip']} {code} {_COUNT} "
            f"{ends['sender_ip']}",
            timeout=15,
        )
    runtime.exec(ends["receiver"], "sleep 8")
    counts = {}
    for code in (4, 3):
        out = runtime.exec(
            ends["receiver"],
            f"tcpdump -nr {capture} 'icmp[0] == 3 and icmp[1] == {code}' "
            "2>/dev/null | wc -l",
        )
        counts[f"code{code}"] = int((out.strip().splitlines() or ["0"])[-1])
    return counts


def frag_needed_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    ends = _endpoints(problem.runtime, params.host_name)
    problem._audit_icmp_ends = ends
    counts = _delivered(problem.runtime, ends)
    ok = counts["code4"] >= _COUNT and counts["code3"] >= _COUNT
    return ok, {**ends, "sent_per_code": _COUNT, **counts}


def frag_needed_filtered(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Fragmentation Needed no longer crosses the node; other ICMP still does."""
    ends = getattr(problem, "_audit_icmp_ends", None) or _endpoints(
        problem.runtime, params.host_name
    )
    counts = _delivered(problem.runtime, ends)
    control_ok = counts["code3"] >= _COUNT
    verified = counts["code4"] == 0 and control_ok
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={**ends, "sent_per_code": _COUNT, **counts, "control_ok": control_ok},
    )
