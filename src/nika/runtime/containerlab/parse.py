"""Parse Containerlab topology YAML into LabSpec."""

from __future__ import annotations

from pathlib import Path

import yaml

from nika.runtime.spec import LabSpec, LinkSpec, NodeSpec


def parse_clab_topology(path: Path | str | dict) -> LabSpec:
    """Parse a Containerlab topology file or document into ``LabSpec``."""
    if isinstance(path, dict):
        data = path
        path = "<memory>"
    else:
        path = Path(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(
            f"Invalid containerlab topology at {path}: expected mapping at root."
        )

    lab_name = str(data.get("name") or "")

    topology = data.get("topology")
    if not isinstance(topology, dict):
        raise ValueError(
            f"Invalid containerlab topology at {path}: missing topology section."
        )

    raw_nodes = topology.get("nodes") or {}
    if not isinstance(raw_nodes, dict):
        raise ValueError(
            f"Invalid containerlab topology at {path}: topology.nodes must be a mapping."
        )

    nodes: list[NodeSpec] = []
    for node_name, node_cfg in raw_nodes.items():
        if not isinstance(node_cfg, dict):
            raise ValueError(f"Invalid node {node_name!r} in {path}: expected mapping.")
        binds = [str(item) for item in (node_cfg.get("binds") or [])]
        exec_cmds = [str(item) for item in (node_cfg.get("exec") or [])]
        nodes.append(
            NodeSpec(
                name=str(node_name),
                image=str(node_cfg.get("image") or ""),
                kind=str(node_cfg.get("kind") or "linux"),
                binds=binds,
                exec_cmds=exec_cmds,
            )
        )

    raw_links = topology.get("links") or []
    if not isinstance(raw_links, list):
        raise ValueError(
            f"Invalid containerlab topology at {path}: topology.links must be a list."
        )

    links: list[LinkSpec] = []
    for idx, link_cfg in enumerate(raw_links):
        if not isinstance(link_cfg, dict):
            raise ValueError(
                f"Invalid link at index {idx} in {path}: expected mapping."
            )
        endpoints = link_cfg.get("endpoints")
        if not isinstance(endpoints, list) or len(endpoints) != 2:
            raise ValueError(
                f"Invalid link at index {idx} in {path}: endpoints must be a list of two strings."
            )
        links.append(LinkSpec(endpoints=(str(endpoints[0]), str(endpoints[1]))))

    return LabSpec(name=lab_name, nodes=nodes, links=links)
