"""Scenario-declared probe expectations for fleet-wide reachability probes.

A scenario declares which endpoint pairs its design keeps isolated and which
round-trip time its design implies. Probes such as PingMesh read this so that
designed isolation and WAN latency are not reported as faults, while an
isolated pair that becomes reachable is still reported.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

PROBE_INTENT_METADATA_KEY = "probe_intent"

# A latency threshold must leave headroom over the design RTT for jitter.
DESIGN_RTT_HEADROOM = 1.5


class ProbeIntent(BaseModel):
    """Designed reachability and latency expectations between endpoints."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Each partition is a tuple of node groups. Nodes in different groups of the
    # same partition are isolated by design; nodes in the same group, and nodes
    # outside the partition, are expected to communicate.
    isolation_partitions: tuple[tuple[tuple[str, ...], ...], ...] = ()
    # Largest endpoint-to-endpoint RTT the design produces (e.g. WAN delay).
    max_expected_rtt_ms: float | None = Field(default=None, gt=0)

    @classmethod
    def from_partitions(
        cls,
        *partitions: Iterable[Iterable[str]],
        max_expected_rtt_ms: float | None = None,
    ) -> "ProbeIntent | None":
        """Build an intent, dropping partitions with fewer than two groups."""
        kept = tuple(
            tuple(tuple(sorted(group)) for group in groups if group)
            for groups in map(list, partitions)
        )
        kept = tuple(groups for groups in kept if len(groups) >= 2)
        if not kept and max_expected_rtt_ms is None:
            return None
        return cls(isolation_partitions=kept, max_expected_rtt_ms=max_expected_rtt_ms)

    def isolation_matcher(self) -> Callable[[str, str], bool]:
        """Return ``f(a, b)``: True when the design isolates ``a`` from ``b``."""
        indexes: list[dict[str, int]] = [
            {node: i for i, group in enumerate(groups) for node in group}
            for groups in self.isolation_partitions
        ]

        def isolated(a: str, b: str) -> bool:
            for index in indexes:
                ga, gb = index.get(a), index.get(b)
                if ga is not None and gb is not None and ga != gb:
                    return True
            return False

        return isolated

    def latency_threshold_ms(self, requested_ms: float) -> float:
        """Raise ``requested_ms`` to the design RTT plus headroom, never lower it."""
        if self.max_expected_rtt_ms is None:
            return requested_ms
        return max(requested_ms, self.max_expected_rtt_ms * DESIGN_RTT_HEADROOM)

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any] | None) -> "ProbeIntent | None":
        raw = (metadata or {}).get(PROBE_INTENT_METADATA_KEY)
        return cls.model_validate(raw) if raw else None
