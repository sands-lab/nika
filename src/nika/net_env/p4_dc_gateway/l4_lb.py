"""Gateway L4 load balancer: a small reference model and live agent controls.

``L4GatewayState`` keeps the ConnTable/pool-version state machine small for
offline reasoning.  The runtime helpers drive the gateway-local agent
(``lb_agent.py``) that learns connections and performs pool updates.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import json
import shlex

from nika.runtime.base import LabRuntime

from .topology_model import (
    CONN_TABLE_CAPACITY,
    CONNECTION_LEARNING_DELAY_MS,
    LB_AGENT,
    LB_AGENT_CONFIG,
    LB_AGENT_STATUS,
    VIP_IP,
    VIP_PORT,
)


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


def set_agent_config(runtime: LabRuntime, gateway: str, **values: int) -> None:
    """Change gateway agent settings, keeping the others (read once a second)."""
    raw = runtime.exec(gateway, f"cat {LB_AGENT_CONFIG} 2>/dev/null", timeout=20)
    try:
        config = json.loads(raw)
    except ValueError:
        config = {}
    if not isinstance(config, dict):
        config = {}
    config.update({key: int(value) for key, value in values.items()})
    text = json.dumps(config, indent=2)
    runtime.exec(
        gateway,
        f"printf '%s\\n' {shlex.quote(text)} > {LB_AGENT_CONFIG}.tmp && "
        f"mv {LB_AGENT_CONFIG}.tmp {LB_AGENT_CONFIG}",
        timeout=20,
    )


def learner_status(runtime: LabRuntime, gateway: str) -> dict:
    """Live state of the gateway learner, with the age of its last heartbeat."""
    raw = runtime.exec(
        gateway, f"cat {LB_AGENT_STATUS} 2>/dev/null; echo; date +%s.%N", timeout=20
    )
    lines = [line for line in raw.splitlines() if line.strip()]
    try:
        status = json.loads(lines[0])
        status["age_sec"] = round(float(lines[-1]) - float(status["time"]), 2)
    except (IndexError, KeyError, TypeError, ValueError):
        return {"error": "learner_status_unavailable", "raw": raw[-300:]}
    return status


def routine_pool_update(runtime: LabRuntime, gateway: str, device_id: int) -> dict:
    """Flip the VIP to the other DIP pool version through the gateway agent."""
    raw = runtime.exec(
        gateway,
        f"python3 {LB_AGENT} update --device-id {device_id} "
        f"--vip {VIP_IP} --vip-port {VIP_PORT}",
        timeout=30,
    )
    try:
        return json.loads(raw.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return {"ok": False, "error": raw[-300:]}
