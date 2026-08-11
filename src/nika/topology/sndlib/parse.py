"""Parse SNDlib network XML into topology IR."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from nika.topology.errors import (
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
)
from nika.topology.sndlib.normalize import normalize_topology
from nika.topology.sndlib.validate import validate_topology

SNDLIB_NS = "http://sndlib.zib.de/network"
_NS = {"snd": SNDLIB_NS}


def _local_name(tag: str) -> str:
    if tag.startswith("{"):
        return tag.rsplit("}", 1)[-1]
    return tag


def _child(parent: ET.Element, name: str) -> ET.Element | None:
    return parent.find(f"snd:{name}", _NS)


def _children(parent: ET.Element, name: str) -> list[ET.Element]:
    return list(parent.findall(f"snd:{name}", _NS))


def _require_child(
    parent: ET.Element, name: str, *, topology: str | None
) -> ET.Element:
    element = _child(parent, name)
    if element is None:
        raise SndlibValidationError(
            f"missing required element {name!r}",
            topology=topology,
        )
    return element


def _text(element: ET.Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    text = element.text.strip()
    return text if text else None


def _require_text(parent: ET.Element, name: str, *, topology: str | None) -> str:
    value = _text(_child(parent, name))
    if value is None:
        raise SndlibValidationError(
            f"missing required text for element {name!r}",
            topology=topology,
        )
    return value


def _parse_float(
    parent: ET.Element, name: str, *, topology: str | None, required: bool = False
) -> float | None:
    raw = _text(_child(parent, name))
    if raw is None:
        if required:
            raise SndlibValidationError(
                f"missing required numeric element {name!r}",
                topology=topology,
            )
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise SndlibParseError(
            f"invalid float for {name!r}: {raw!r}",
            topology=topology,
        ) from exc


def _parse_int(parent: ET.Element, name: str, *, topology: str | None) -> int | None:
    raw = _text(_child(parent, name))
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise SndlibParseError(
            f"invalid int for {name!r}: {raw!r}",
            topology=topology,
        ) from exc


def _parse_meta(root: ET.Element) -> dict[str, str]:
    meta_el = _child(root, "meta")
    if meta_el is None:
        return {}
    meta: dict[str, str] = {}
    for child in list(meta_el):
        key = _local_name(child.tag)
        value = (child.text or "").strip()
        meta[key] = value
    return meta


def _parse_node(element: ET.Element, *, topology: str | None) -> TopoNode:
    node_id = element.get("id")
    if not node_id:
        raise SndlibValidationError("node missing id attribute", topology=topology)
    coords = _child(element, "coordinates")
    x = y = None
    if coords is not None:
        x = _parse_float(coords, "x", topology=topology, required=True)
        y = _parse_float(coords, "y", topology=topology, required=True)
    return TopoNode(id=node_id, x=x, y=y)


def _parse_capacity_module(
    element: ET.Element, *, topology: str | None
) -> CapacityModule:
    capacity = _parse_float(element, "capacity", topology=topology, required=True)
    cost = _parse_float(element, "cost", topology=topology, required=True)
    assert capacity is not None and cost is not None
    return CapacityModule(capacity=capacity, cost=cost)


def _parse_link(element: ET.Element, *, topology: str | None) -> TopoLink:
    link_id = element.get("id")
    if not link_id:
        raise SndlibValidationError("link missing id attribute", topology=topology)
    source = _require_text(element, "source", topology=topology)
    target = _require_text(element, "target", topology=topology)
    routing_cost = _parse_float(element, "routingCost", topology=topology)
    setup_cost = _parse_float(element, "setupCost", topology=topology)

    preinstalled_el = _child(element, "preInstalledModule")
    preinstalled = None
    if preinstalled_el is not None:
        preinstalled = _parse_capacity_module(preinstalled_el, topology=topology)

    modules: list[CapacityModule] = []
    additional_el = _child(element, "additionalModules")
    if additional_el is not None:
        for module_el in _children(additional_el, "addModule"):
            modules.append(_parse_capacity_module(module_el, topology=topology))

    return TopoLink(
        id=link_id,
        source=source,
        target=target,
        routing_cost=routing_cost,
        setup_cost=setup_cost,
        preinstalled=preinstalled,
        additional_modules=tuple(modules),
    )


def _parse_admissible_path(
    element: ET.Element, *, topology: str | None
) -> AdmissiblePath:
    path_id = element.get("id")
    if not path_id:
        raise SndlibValidationError(
            "admissiblePath missing id attribute", topology=topology
        )
    link_ids = tuple(
        text
        for link_el in _children(element, "linkId")
        if (text := _text(link_el)) is not None
    )
    if not link_ids:
        raise SndlibValidationError(
            f"admissiblePath {path_id!r} has no linkId entries",
            topology=topology,
        )
    return AdmissiblePath(id=path_id, link_ids=link_ids)


def _parse_demand(element: ET.Element, *, topology: str | None) -> TopoDemand:
    demand_id = element.get("id")
    if not demand_id:
        raise SndlibValidationError("demand missing id attribute", topology=topology)
    source = _require_text(element, "source", topology=topology)
    target = _require_text(element, "target", topology=topology)
    demand_value = _parse_float(
        element, "demandValue", topology=topology, required=True
    )
    assert demand_value is not None
    routing_unit = _parse_int(element, "routingUnit", topology=topology)
    max_path_length = _parse_int(element, "maxPathLength", topology=topology)

    paths: list[AdmissiblePath] = []
    paths_el = _child(element, "admissiblePaths")
    if paths_el is not None:
        for path_el in _children(paths_el, "admissiblePath"):
            paths.append(_parse_admissible_path(path_el, topology=topology))

    return TopoDemand(
        id=demand_id,
        source=source,
        target=target,
        demand_value=demand_value,
        routing_unit=routing_unit,
        max_path_length=max_path_length,
        admissible_paths=tuple(paths),
    )


def _reject_native_ascii(raw: str, *, topology: str | None) -> None:
    first = next((line.strip() for line in raw.splitlines() if line.strip()), "")
    if first.startswith("?SNDlib native format"):
        raise SndlibUnsupportedError(
            "native ASCII SNDlib format is not supported; provide XML network files",
            topology=topology,
        )


def _read_xml_text(path: Path, *, topology: str | None) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="iso-8859-1")
    except OSError as exc:
        raise SndlibParseError(
            f"cannot read SNDlib XML: {exc}", topology=topology
        ) from exc


def parse_sndlib_xml(
    source: str | Path,
    *,
    name: str | None = None,
) -> NetworkTopology:
    """Parse a SNDlib network XML file or XML string into NetworkTopology."""
    if isinstance(source, Path):
        topology_name = name or source.parent.name or source.stem
        raw = _read_xml_text(source, topology=topology_name)
    else:
        path = Path(source)
        if "\n" not in source and path.is_file():
            topology_name = name or path.parent.name or path.stem
            raw = _read_xml_text(path, topology=topology_name)
        else:
            topology_name = name or "inline"
            raw = source

    _reject_native_ascii(raw, topology=topology_name)

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SndlibParseError(f"invalid XML: {exc}", topology=topology_name) from exc

    if _local_name(root.tag) != "network":
        raise SndlibUnsupportedError(
            f"unsupported root element {_local_name(root.tag)!r}; "
            "expected SNDlib <network> XML",
            topology=topology_name,
        )

    # Detect accidental model/solution payloads by looking for non-network sections.
    for unsupported in ("solution", "model"):
        if _child(root, unsupported) is not None:
            raise SndlibUnsupportedError(
                f"SNDlib {unsupported} documents are not supported; "
                "expected a network definition",
                topology=topology_name,
            )

    meta = _parse_meta(root)
    structure = _require_child(root, "networkStructure", topology=topology_name)
    nodes_el = _require_child(structure, "nodes", topology=topology_name)
    links_el = _require_child(structure, "links", topology=topology_name)
    demands_el = _child(root, "demands")

    nodes = [
        _parse_node(el, topology=topology_name) for el in _children(nodes_el, "node")
    ]
    links = [
        _parse_link(el, topology=topology_name) for el in _children(links_el, "link")
    ]
    demands: list[TopoDemand] = []
    if demands_el is not None:
        demands = [
            _parse_demand(el, topology=topology_name)
            for el in _children(demands_el, "demand")
        ]

    topology = normalize_topology(
        name=topology_name,
        meta=meta,
        nodes=nodes,
        links=links,
        demands=demands,
    )
    validate_topology(topology)
    return topology
