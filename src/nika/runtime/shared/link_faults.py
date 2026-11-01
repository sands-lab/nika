"""Traffic-control commands for link faults applied outside lab nodes."""

from __future__ import annotations


def corrupt_commands(dev: str, percentage: int) -> list[str]:
    """Corrupt IP frames leaving ``dev`` and drop other frames at the same rate.

    A real link's FCS discards every damaged frame. netem has no FCS: it
    delivers the damaged frame, and only IP-family receivers catch it with
    their own checksums. ARP has none, so a delivered bit flip in an ARP reply
    would poison the peer's neighbor cache and cut the link for a
    reachable-time. Non-IP frames are therefore lost instead of corrupted.
    """
    return [
        f"tc qdisc replace dev {dev} root handle 1: prio bands 2 "
        f"priomap {' '.join(['0'] * 16)}",
        f"tc qdisc replace dev {dev} parent 1:1 handle 10: netem loss {percentage}%",
        f"tc qdisc replace dev {dev} parent 1:2 handle 20: netem corrupt {percentage}%",
        *(
            f"tc filter replace dev {dev} parent 1: protocol {proto} "
            f"prio {prio} u32 match u32 0 0 flowid 1:2"
            for prio, proto in ((1, "ip"), (2, "ipv6"))
        ),
    ]
