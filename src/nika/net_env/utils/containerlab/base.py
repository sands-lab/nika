"""Base class for Containerlab-backed network environments."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import ClassVar

from nika.config import RUNTIME_DIR
from nika.net_env.base import NetworkEnvBase
from nika.runtime.containerlab.parse import parse_clab_topology
from nika.runtime.containerlab.render import render_topology
from nika.runtime.spec import LabSpec


class ContainerlabNetworkEnv(NetworkEnvBase):
    """Derive topology from a sibling ``{LAB_NAME}.clab.yml.tmpl`` file."""

    LAB_NAME: ClassVar[str]
    DESC: ClassVar[str]
    TOPO_LEVEL: ClassVar[str] = "easy"
    TOPO_SIZE: ClassVar[int | None] = None
    TAGS: ClassVar[list[str]] = []
    SUPPORTED_BACKENDS: ClassVar[list[str]] = ["containerlab"]

    def __init__(self, *, backend: str = "containerlab", **kwargs):
        super().__init__(backend=backend, **kwargs)
        self.name = self.LAB_NAME
        self.desc = self.DESC

    @property
    def lab_dir(self) -> Path:
        return Path(inspect.getfile(type(self))).resolve().parent

    def topology_template(self) -> Path:
        return self.lab_dir / f"{self.LAB_NAME}.clab.yml.tmpl"

    def _prepare_runtime_files(self) -> None:
        lab_name = self.name
        if not lab_name:
            raise ValueError("Lab name is required before deploy.")
        self.runtime_workdir = RUNTIME_DIR / "containerlab" / lab_name
        self.runtime_workdir.mkdir(parents=True, exist_ok=True)

        self.topology_file = self.runtime_workdir / f"{self.LAB_NAME}.clab.yml"
        render_topology(
            self.topology_template(), lab_name=lab_name, output_path=self.topology_file
        )

    def get_lab_spec(self) -> LabSpec:
        spec = parse_clab_topology(self.topology_template())
        spec.name = self.name or self.LAB_NAME
        return spec

    def get_topology(self) -> list[tuple[str, str]]:
        return [link.endpoints for link in self.get_lab_spec().links]

    def _has_role_inventory(self) -> bool:
        return bool(
            self.hosts
            or self.routers
            or self.switches
            or self.bmv2_switches
            or self.ovs_switches
            or self.servers
        )

    def get_info(self) -> str:
        """
        Agent brief: description + inventory + topology edges.

        Prefer declared role lists when the scenario populated them (ISP and
        similar). Fall back to lab-spec node/link names for template labs.
        """
        topo = self.get_topology()
        topo_line = f"Topology: {', '.join(f'({a}, {b})' for a, b in topo)}"
        if self._has_role_inventory():
            return (
                f"Network Description: {self.desc}\n"
                f"{self._format_role_inventory_lines()}"
                f"{topo_line}"
            )
        spec = self.get_lab_spec()
        node_names = [node.name for node in spec.nodes]
        link_labels = [f"{a} <-> {b}" for a, b in topo]
        summary = f"Network Description: {self.desc}\n"
        summary += f"Nodes: {', '.join(node_names)}\n"
        summary += f"Links: {', '.join(link_labels)}\n"
        summary += topo_line
        return summary
