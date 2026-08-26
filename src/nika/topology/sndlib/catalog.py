"""Discover and load vendored SNDlib topologies under net_env/isp/sndlib."""

from __future__ import annotations

from pathlib import Path

from nika.topology.errors import SndlibParseError, SndlibUnsupportedError
from nika.topology.models import NetworkTopology
from nika.topology.sndlib.parse import parse_sndlib_xml

# Official sndlib-networks-xml archive (26 networks).
SNDLIB_TOPOLOGY_NAMES: tuple[str, ...] = (
    "abilene",
    "atlanta",
    "brain",
    "cost266",
    "dfn-bwin",
    "dfn-gwin",
    "di-yuan",
    "france",
    "geant",
    "germany50",
    "giul39",
    "india35",
    "janos-us",
    "janos-us-ca",
    "newyork",
    "nobel-eu",
    "nobel-germany",
    "nobel-us",
    "norway",
    "pdh",
    "pioro40",
    "polska",
    "sun",
    "ta1",
    "ta2",
    "zib54",
)

SNDLIB_TOPOLOGY_SIZE_DEFAULTS: dict[str, str] = {
    "s": "abilene",
    "m": "france",
    "l": "pioro40",
}

# The current SNDlib catalog ordered by ``(node_count, topology_name)`` and
# split as evenly as possible into the same relative s/m/l sizes as NIKA's
# generated scenarios. Keep this explicit so adding an asset cannot silently
# change the meaning of a published benchmark case.
SNDLIB_TOPOLOGY_TIERS: dict[str, tuple[str, ...]] = {
    "s": (
        "dfn-bwin",
        "dfn-gwin",
        "di-yuan",
        "pdh",
        "abilene",
        "polska",
        "nobel-us",
        "atlanta",
    ),
    "m": (
        "newyork",
        "nobel-germany",
        "geant",
        "ta1",
        "france",
        "janos-us",
        "norway",
        "sun",
        "nobel-eu",
    ),
    "l": (
        "india35",
        "cost266",
        "giul39",
        "janos-us-ca",
        "pioro40",
        "germany50",
        "zib54",
        "ta2",
        "brain",
    ),
}

_NETWORK_FILENAME = "network.xml"


def sndlib_data_root() -> Path:
    """Return the directory that holds per-topology SNDlib scenario assets."""
    return Path(__file__).resolve().parents[2] / "net_env" / "isp" / "sndlib"


def topology_network_path(name: str) -> Path:
    return sndlib_data_root() / name / _NETWORK_FILENAME


def list_sndlib_topologies() -> list[str]:
    """Return sorted topology names that have a vendored network.xml."""
    root = sndlib_data_root()
    if not root.is_dir():
        return []
    names = sorted(
        path.name
        for path in root.iterdir()
        if path.is_dir() and (path / _NETWORK_FILENAME).is_file()
    )
    return names


def topologies_for_size(topo_size: str) -> tuple[str, ...]:
    """Return the fixed SNDlib candidates for a relative size tier."""
    try:
        return SNDLIB_TOPOLOGY_TIERS[topo_size]
    except KeyError as exc:
        raise ValueError("Topology size must be one of: s, m, l.") from exc


def topology_size_for_name(topology: str) -> str:
    """Return the relative SNDlib size tier that contains ``topology``."""
    for topo_size, names in SNDLIB_TOPOLOGY_TIERS.items():
        if topology in names:
            return topo_size
    raise ValueError(f"Unknown SNDlib topology {topology!r}.")


def topology_for_size(topo_size: str) -> str:
    """Return the deterministic SNDlib representative for ``s``, ``m``, or ``l``."""
    try:
        return SNDLIB_TOPOLOGY_SIZE_DEFAULTS[topo_size]
    except KeyError as exc:
        raise ValueError("Topology size must be one of: s, m, l.") from exc


def load_sndlib_topology(name_or_path: str | Path) -> NetworkTopology:
    """Load a vendored topology by name, or parse an explicit XML path."""
    if isinstance(name_or_path, Path):
        path = name_or_path
        if not path.is_file():
            raise SndlibParseError(f"SNDlib XML not found: {path}")
        return parse_sndlib_xml(path, name=path.parent.name or path.stem)

    # Prefer catalog name when it matches a vendored topology.
    catalog_path = topology_network_path(name_or_path)
    if catalog_path.is_file():
        return parse_sndlib_xml(catalog_path, name=name_or_path)

    path = Path(name_or_path)
    if path.is_file():
        return parse_sndlib_xml(path, name=path.parent.name or path.stem)

    raise SndlibUnsupportedError(
        f"unknown SNDlib topology {name_or_path!r}; "
        f"expected a catalog name or path to {_NETWORK_FILENAME}"
    )
