"""Deterministic control-plane model for the gateway L4 load balancer.

The model deliberately keeps the state-machine small: it is used to build
P4Runtime intent and to make benchmark connection selection reproducible.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib

from .topology_model import CONN_TABLE_CAPACITY, CONNECTION_LEARNING_DELAY_MS


FiveTuple = tuple[str, str, int, int, int]


def bucket_for(flow: FiveTuple, buckets: int = 64) -> int:
    raw = "|".join(str(part) for part in flow).encode()
    return int.from_bytes(hashlib.blake2s(raw, digest_size=4).digest(), "big") % buckets


@dataclass
class L4GatewayState:
    """Capacity-bounded connection mappings and a minimal TransitTable."""

    capacity: int = CONN_TABLE_CAPACITY
    learning_delay_ms: int = CONNECTION_LEARNING_DELAY_MS
    pool_version: int = 1
    connections: OrderedDict[FiveTuple, str] = field(default_factory=OrderedDict)
    pending: dict[FiveTuple, int] = field(default_factory=dict)

    def learn(self, flow: FiveTuple, dip: str) -> FiveTuple | None:
        """Insert a mapping, naturally evicting the oldest mapping at capacity."""
        evicted = None
        if flow in self.connections:
            self.connections.move_to_end(flow)
        elif len(self.connections) >= self.capacity:
            evicted, _ = self.connections.popitem(last=False)
        self.connections[flow] = dip
        self.pending.pop(flow, None)
        return evicted

    def begin_update(self) -> None:
        for flow in self.pending:
            self.pending[flow] = self.pool_version

    def unsafe_update(self) -> None:
        self.pool_version += 1

    def safe_update(self) -> None:
        self.begin_update()
        self.pool_version += 1

    def version_for(self, flow: FiveTuple) -> int:
        return self.pending.get(flow, self.pool_version)

    @property
    def occupancy(self) -> int:
        return len(self.connections)
