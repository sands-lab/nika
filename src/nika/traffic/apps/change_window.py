#!/usr/bin/env python3
"""Routine operator housekeeping on an FRR router, like a change window.

Every so often it applies one benign configuration edit through ``vtysh``:
an interface description is set or cleared, or an entry of a reference
prefix-list that no route-map uses is added or removed. The edits go through
mgmtd, so they show up in ``show mgmt commit-history`` like any other
operator commit, but they never change forwarding or protocol state.
The first few edits come shortly after start, later ones every ``--interval``
seconds (random, seeded). The running config is not saved.
"""

from __future__ import annotations

import argparse
import random
import re
import subprocess
import time

DESCRIPTIONS = (
    "uplink",
    "core link",
    "peering",
    "backup path",
    "customer handoff",
    "reviewed by noc",
    "circuit under watch",
    "inter-pop trunk",
)
# Reference list nothing refers to (RFC 5737 documentation ranges).
REFERENCE_LIST = "REF-DOCUMENTATION"
REFERENCE_ENTRIES = (
    (5, "192.0.2.0/24"),
    (10, "198.51.100.0/24"),
    (15, "203.0.113.0/24"),
)
# Edits applied right after start, so the history already has entries.
WARMUP_EDITS = 3
WARMUP_GAP = (3.0, 30.0)
VTYSH_TIMEOUT = 20


def seconds_range(text: str) -> tuple[float, float]:
    low, _, high = text.partition("-")
    return float(low), float(high or low)


def interfaces_from_config(running_config: str) -> list[str]:
    """Interface names in ``show running-config``, loopbacks excluded."""
    names = re.findall(r"^interface (\S+)\s*$", running_config, flags=re.MULTILINE)
    return [name for name in names if name != "lo"]


def choose_change(
    rng: random.Random,
    interfaces: list[str],
    described: dict[str, str],
    listed: set[int],
) -> list[str]:
    """Pick the next edit as ``conf t`` lines and update the tracked state."""
    if interfaces and rng.random() < 0.6:
        name = rng.choice(interfaces)
        if name in described and rng.random() < 0.5:
            del described[name]
            return [f"interface {name}", "no description"]
        text = rng.choice([d for d in DESCRIPTIONS if d != described.get(name)])
        described[name] = text
        return [f"interface {name}", f"description {text}"]
    free = [(seq, net) for seq, net in REFERENCE_ENTRIES if seq not in listed]
    if listed and (not free or rng.random() < 0.5):
        seq, net = rng.choice([e for e in REFERENCE_ENTRIES if e[0] in listed])
        listed.discard(seq)
        return [f"no ip prefix-list {REFERENCE_LIST} seq {seq} permit {net}"]
    seq, net = rng.choice(free)
    listed.add(seq)
    return [f"ip prefix-list {REFERENCE_LIST} seq {seq} permit {net}"]


def vtysh(*lines: str) -> str:
    args = ["vtysh"]
    for line in lines:
        args += ["-c", line]
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=VTYSH_TIMEOUT, check=False
    )
    return result.stdout


def run(interval: tuple[float, float], seed: int) -> None:
    rng = random.Random(seed)
    described: dict[str, str] = {}
    listed: set[int] = set()
    interfaces: list[str] = []
    applied = 0
    while True:
        gap = rng.uniform(*(WARMUP_GAP if applied < WARMUP_EDITS else interval))
        time.sleep(gap)
        try:
            if not interfaces:
                interfaces = interfaces_from_config(vtysh("show running-config"))
            vtysh(
                "configure terminal", *choose_change(rng, interfaces, described, listed)
            )
            applied += 1
        except (OSError, subprocess.SubprocessError):
            continue


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", default="300-1200", help="seconds between edits")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    run(seconds_range(args.interval), args.seed)


if __name__ == "__main__":
    main()
