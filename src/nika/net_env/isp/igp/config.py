"""ISP compile configuration."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Network
from pathlib import Path
from typing import Literal

from nika.net_env.isp.igp.errors import IspConfigError
from nika.topology.sndlib.catalog import SNDLIB_TOPOLOGY_NAMES, topology_network_path

IgpProtocol = Literal["isis", "ospf"]
MetricStrategy = Literal["constant", "routing_cost", "inv_capacity"]

SUPPORTED_IGPS: tuple[IgpProtocol, ...] = ("isis", "ospf")
SUPPORTED_METRIC_STRATEGIES: tuple[MetricStrategy, ...] = (
    "constant",
    "routing_cost",
    "inv_capacity",
)

DEFAULT_TOPO = "polska"
DEFAULT_IGP: IgpProtocol = "isis"
DEFAULT_METRIC_STRATEGY: MetricStrategy = "constant"
DEFAULT_CONSTANT_METRIC = 10
DEFAULT_LOOPBACK_POOL = IPv4Network("10.255.0.0/16")
DEFAULT_P2P_POOL = IPv4Network("10.0.0.0/8")
# Used by inv_capacity: metric = max(1, round(REFERENCE / capacity)).
DEFAULT_INV_CAPACITY_REFERENCE = 1_000_000.0


@dataclass(frozen=True)
class IspConfig:
    """Parameters that control SNDlib → ISP plan compilation."""

    topology: str | Path = DEFAULT_TOPO
    igp: IgpProtocol = DEFAULT_IGP
    metric_strategy: MetricStrategy = DEFAULT_METRIC_STRATEGY
    constant_metric: int = DEFAULT_CONSTANT_METRIC
    loopback_pool: IPv4Network = DEFAULT_LOOPBACK_POOL
    p2p_pool: IPv4Network = DEFAULT_P2P_POOL
    inv_capacity_reference: float = DEFAULT_INV_CAPACITY_REFERENCE

    def validated(self) -> IspConfig:
        """Return self after validating option combinations and pools."""
        if self.igp not in SUPPORTED_IGPS:
            raise IspConfigError(
                f"Unsupported IGP {self.igp!r}; expected one of {SUPPORTED_IGPS}."
            )
        if self.metric_strategy not in SUPPORTED_METRIC_STRATEGIES:
            raise IspConfigError(
                f"Unsupported metric strategy {self.metric_strategy!r}; "
                f"expected one of {SUPPORTED_METRIC_STRATEGIES}."
            )
        if self.constant_metric < 1:
            raise IspConfigError(
                f"constant_metric must be >= 1, got {self.constant_metric}."
            )
        if self.inv_capacity_reference <= 0:
            raise IspConfigError(
                "inv_capacity_reference must be > 0, "
                f"got {self.inv_capacity_reference}."
            )
        if self.loopback_pool.prefixlen > 32:
            raise IspConfigError(f"Invalid loopback_pool {self.loopback_pool}.")
        if self.p2p_pool.prefixlen > 31:
            raise IspConfigError(
                f"p2p_pool {self.p2p_pool} is too small for /31 links."
            )
        _validate_topology_ref(self.topology)
        return self


def _validate_topology_ref(topology: str | Path) -> None:
    if isinstance(topology, Path):
        if not topology.is_file():
            raise IspConfigError(f"Topology file not found: {topology}")
        return
    catalog_path = topology_network_path(topology)
    if catalog_path.is_file():
        return
    path = Path(topology)
    if path.is_file():
        return
    known = ", ".join(SNDLIB_TOPOLOGY_NAMES)
    raise IspConfigError(
        f"Unknown SNDlib topology {topology!r}. "
        f"Pass a catalog name ({known}) or an existing network.xml path."
    )
