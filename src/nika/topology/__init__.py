"""Backend-agnostic network topology import and IR."""

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
from nika.topology.sndlib.catalog import list_sndlib_topologies, load_sndlib_topology

__all__ = [
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
    "list_sndlib_topologies",
    "load_sndlib_topology",
]
