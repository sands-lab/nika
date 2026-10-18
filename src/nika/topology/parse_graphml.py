"""Parse Internet Topology Zoo GraphML into topology IR."""

from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path

from nika.topology.errors import (
    SndlibParseError,
    SndlibUnsupportedError,
    SndlibValidationError,
)
from nika.topology.models import CapacityModule, NetworkTopology, TopoLink, TopoNode
from nika.topology.normalize import normalize_topology
from nika.topology.validate import validate_topology

GRAPHML_NS = "http://graphml.graphdrawing.org/xmlns"
_NS = {"g": GRAPHML_NS}
_SLUG_RE = re.compile(r"[^a-z0-9]+")
# Device names become FRR hostnames. ``show isis neighbor`` prints the hostname
# in a 20-character column, and longer names run into the interface column.
_MAX_NODE_SLUG = 19


def _truncate(slug: str, limit: int) -> str:
    return slug[:limit].rstrip("_")


def _node_slug(label: str | None, graphml_id: str) -> str:
    folded = unicodedata.normalize("NFKD", label or "")
    ascii_label = folded.encode("ascii", "ignore").decode("ascii")
    slug = _truncate(_SLUG_RE.sub("_", ascii_label.lower()).strip("_"), _MAX_NODE_SLUG)
    if not slug:
        slug = f"n{_SLUG_RE.sub('_', graphml_id.lower()).strip('_')}"
    if slug[0].isdigit():
        slug = _truncate(f"n_{slug}", _MAX_NODE_SLUG)
    return slug


def _data(element: ET.Element, keys: dict[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for data_el in element.findall("g:data", _NS):
        name = keys.get(data_el.get("key", ""))
        if name is not None:
            values[name] = (data_el.text or "").strip()
    return values


def _float_or_none(raw: str | None, *, field: str, topology: str) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise SndlibParseError(
            f"invalid float for {field!r}: {raw!r}", topology=topology
        ) from exc


def _largest_component(node_ids: list[str], edges: list[tuple[str, str]]) -> set[str]:
    """Largest connected component; ties go to the earliest node in file order."""
    graph: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    for source, target in edges:
        graph[source].add(target)
        graph[target].add(source)
    best: set[str] = set()
    seen: set[str] = set()
    for root in node_ids:
        if root in seen:
            continue
        component = {root}
        queue = [root]
        for current in queue:
            for peer in graph[current]:
                if peer not in component:
                    component.add(peer)
                    queue.append(peer)
        seen |= component
        if len(component) > len(best):
            best = component
    return best


def parse_zoo_graphml(
    source: str | Path, *, name: str | None = None
) -> NetworkTopology:
    """Parse a Topology Zoo GraphML file or string into NetworkTopology.

    Only the largest connected component is kept and self-loops are dropped, so
    every router can reach every other one. ``meta["dropped_nodes"]`` records
    how many nodes were removed. Node ids are unique slugs of the GraphML
    ``label``. Parallel edges become separate links. ``LinkSpeedRaw`` (bit/s)
    maps to preinstalled capacity in Mbit/s. Topology Zoo has no demands.
    """
    if isinstance(source, Path):
        topology_name = name or source.stem.lower()
        try:
            raw = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise SndlibParseError(
                f"cannot read Topology Zoo GraphML: {exc}", topology=topology_name
            ) from exc
    else:
        topology_name = name or "inline"
        raw = source

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SndlibParseError(f"invalid XML: {exc}", topology=topology_name) from exc
    if root.tag != f"{{{GRAPHML_NS}}}graphml":
        raise SndlibUnsupportedError(
            f"unsupported root element {root.tag!r}; expected GraphML <graphml>",
            topology=topology_name,
        )
    graph = root.find("g:graph", _NS)
    if graph is None:
        raise SndlibValidationError("GraphML has no <graph>", topology=topology_name)

    keys = {
        key.get("id", ""): key.get("attr.name", "")
        for key in root.findall("g:key", _NS)
        if key.get("attr.name")
    }
    meta = _data(graph, keys)

    labels: dict[str, str | None] = {}
    coords: dict[str, tuple[float | None, float | None]] = {}
    for node_el in graph.findall("g:node", _NS):
        graphml_id = node_el.get("id")
        if not graphml_id:
            raise SndlibValidationError(
                "node missing id attribute", topology=topology_name
            )
        if graphml_id in labels:
            raise SndlibValidationError(
                f"duplicate node id {graphml_id!r}", topology=topology_name
            )
        values = _data(node_el, keys)
        labels[graphml_id] = values.get("label") or None
        coords[graphml_id] = (
            _float_or_none(
                values.get("Longitude"), field="Longitude", topology=topology_name
            ),
            _float_or_none(
                values.get("Latitude"), field="Latitude", topology=topology_name
            ),
        )

    edges: list[tuple[str, str, float | None]] = []
    for edge_el in graph.findall("g:edge", _NS):
        source_id = edge_el.get("source")
        target_id = edge_el.get("target")
        if source_id not in labels or target_id not in labels:
            raise SndlibValidationError(
                f"edge references unknown node {source_id!r} / {target_id!r}",
                topology=topology_name,
            )
        if source_id == target_id:
            continue
        speed = _float_or_none(
            _data(edge_el, keys).get("LinkSpeedRaw"),
            field="LinkSpeedRaw",
            topology=topology_name,
        )
        edges.append((source_id, target_id, speed))

    kept = _largest_component(
        list(labels), [(source_id, target_id) for source_id, target_id, _ in edges]
    )
    meta["dropped_nodes"] = str(len(labels) - len(kept))

    node_id_by_graphml: dict[str, str] = {}
    used: set[str] = set()
    nodes: list[TopoNode] = []
    for graphml_id, label in labels.items():
        if graphml_id not in kept:
            continue
        base = _node_slug(label, graphml_id)
        node_id = base
        suffix = 2
        while node_id in used:
            tail = f"_{suffix}"
            node_id = f"{_truncate(base, _MAX_NODE_SLUG - len(tail))}{tail}"
            suffix += 1
        used.add(node_id)
        node_id_by_graphml[graphml_id] = node_id
        x, y = coords[graphml_id]
        nodes.append(TopoNode(id=node_id, x=x, y=y))

    links: list[TopoLink] = []
    for source_id, target_id, speed in edges:
        if source_id not in kept:
            continue
        links.append(
            TopoLink(
                id=f"L{len(links) + 1}",
                source=node_id_by_graphml[source_id],
                target=node_id_by_graphml[target_id],
                preinstalled=(
                    CapacityModule(capacity=speed / 1e6, cost=0.0)
                    if speed is not None and speed > 0
                    else None
                ),
            )
        )

    topology = normalize_topology(
        name=topology_name,
        meta=meta,
        nodes=nodes,
        links=links,
        demands=[],
        source_format="topology-zoo-graphml",
    )
    validate_topology(topology)
    return topology
