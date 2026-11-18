"""Backend-agnostic network topology import (SNDlib, Topology Zoo) and IR."""

from nika.topology.catalog import ISP_TOPOLOGY_NAMES, list_topologies, load_topology
from nika.topology.errors import (
    SndlibError,
    SndlibParseError,
    SndlibUnsupportedError,
    SndlibValidationError,
)
from nika.topology.models import (
    AdmissiblePath,
    CapacityModule,
    NetworkTopology,
    TopoDemand,
    TopoLink,
    TopoNode,
    link_preinstalled_capacity,
)

__all__ = [
    "ISP_TOPOLOGY_NAMES",
    "AdmissiblePath",
    "CapacityModule",
    "NetworkTopology",
    "SndlibError",
    "SndlibParseError",
    "SndlibUnsupportedError",
    "SndlibValidationError",
    "TopoDemand",
    "TopoLink",
    "TopoNode",
    "link_preinstalled_capacity",
    "list_topologies",
    "load_topology",
]
