"""Plan ethN ↔ Containerlab SRL interface name helpers."""

from __future__ import annotations

import re

_ETH_RE = re.compile(r"^eth(\d+)$")


def eth_index(iface_name: str) -> int:
    """Return N for ``ethN``."""
    match = _ETH_RE.match(iface_name)
    if not match:
        raise ValueError(f"Expected ethN interface name, got {iface_name!r}.")
    return int(match.group(1))


def srl_e1_name(iface_name: str) -> str:
    """Map plan ``ethN`` → Containerlab endpoint ``e1-{N+1}``."""
    return f"e1-{eth_index(iface_name) + 1}"


def srl_ethernet_name(iface_name: str) -> str:
    """Map plan ``ethN`` → SRL ``ethernet-1/{N+1}``."""
    return f"ethernet-1/{eth_index(iface_name) + 1}"


def srl_subinterface(iface_name: str) -> str:
    """Map plan ``ethN`` → SRL ``ethernet-1/{N+1}.0``."""
    return f"{srl_ethernet_name(iface_name)}.0"
