"""Containerlab-backed LabRuntime implementation."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

import docker

from nika.runtime.base import LabRuntime
from nika.runtime.containerlab.parse import parse_clab_topology
from nika.runtime.shared.containers import pause_container, unpause_container
from nika.runtime.shared.execution import exec_with_timeout


class ContainerlabRuntime(LabRuntime):
    """Deploy and manage labs via ``clab`` CLI; exec/fault via Docker SDK."""

    def __init__(
        self,
        *,
        lab_name: str,
        topology_file: Path,
        runtime_workdir: Path | None = None,
    ) -> None:
        self._lab_name = lab_name
        self._topology_file = Path(topology_file)
        self._runtime_workdir = (
            Path(runtime_workdir) if runtime_workdir else self._topology_file.parent
        )
        self._docker = docker.from_env()
        self._node_containers: dict[str, docker.models.containers.Container] = {}
        self._topology_neighbors: dict[str, list[str]] | None = None

    @property
    def backend(self) -> str:
        return "containerlab"

    @property
    def lab_name(self) -> str:
        return self._lab_name

    @property
    def topology_file(self) -> Path:
        return self._topology_file

    @property
    def runtime_workdir(self) -> Path:
        return self._runtime_workdir

    def _run_clab(self, *args: str) -> subprocess.CompletedProcess[str]:
        cmd = ["clab", *args, "--log-level", "error"]
        return subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            cwd=str(self._runtime_workdir),
        )

    @staticmethod
    def _parse_clab_json(raw: str) -> Any:
        start = raw.find("{")
        if start == -1:
            return {}
        return json.loads(raw[start:])

    def _logical_node_name(self, container_name: str) -> str:
        prefix = f"clab-{self._lab_name}-"
        if container_name.startswith(prefix):
            return container_name[len(prefix) :]
        marker = f"-{self._lab_name}-"
        if marker in container_name:
            return container_name.rsplit(marker, 1)[-1]
        return container_name

    def _refresh_node_map(self) -> None:
        result = self._run_clab(
            "inspect",
            "-t",
            str(self._topology_file),
            "--format",
            "json",
        )
        if result.returncode != 0:
            self._node_containers = {}
            return
        payload = self._parse_clab_json(result.stdout or "")
        nodes: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            for value in payload.values():
                if isinstance(value, list):
                    nodes.extend(item for item in value if isinstance(item, dict))
        elif isinstance(payload, list):
            nodes = [item for item in payload if isinstance(item, dict)]
        mapping: dict[str, docker.models.containers.Container] = {}
        for entry in nodes:
            container_name = str(entry.get("name") or "")
            container_id = entry.get("container_id") or entry.get("id")
            if not container_name or not container_id:
                continue
            node_name = self._logical_node_name(container_name)
            mapping[node_name] = self._docker.containers.get(container_id)
        self._node_containers = mapping

    def deploy(self) -> None:
        if self.exists():
            print(f"Lab {self._lab_name} exists")
            return
        result = self._run_clab(
            "deploy",
            "-t",
            str(self._topology_file),
            "--reconfigure",
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            hint = ""
            low = err.lower()
            if any(
                marker in low
                for marker in (
                    "invalid config for network",
                    "subnet already in use",
                    "no configured subnet",
                )
            ):
                try:
                    from nika.workflows.session.close import (
                        remove_orphaned_containerlab_management_network,
                    )

                    remove_orphaned_containerlab_management_network(self._lab_name)
                except Exception:  # noqa: BLE001 - best-effort before re-raise
                    pass
                hint = (
                    f" Leftover Docker network br-{self._lab_name} may be "
                    "blocking deploy; run: nika session wipe -y"
                )
            raise RuntimeError(
                f"clab deploy failed for {self._lab_name}: {err}{hint}"
            )
        time.sleep(5)
        self._refresh_node_map()

    def destroy(self) -> None:
        # Host-side flap workers are outside containerlab's inventory.
        # Remove only workers carrying this lab's opaque identifier before
        # destroying their veth endpoints.
        from nika.service.containerlab.host_tc import HostTcController

        HostTcController.cleanup_lab(self._lab_name)
        result = self._run_clab(
            "destroy",
            "-t",
            str(self._topology_file),
            "--cleanup",
        )
        if result.returncode != 0:
            print(
                f"Error destroying containerlab lab {self._lab_name}: {result.stderr or result.stdout}"
            )
        self._node_containers = {}

    def exists(self) -> bool:
        self._refresh_node_map()
        return bool(self._node_containers)

    def inspect(self) -> list[dict[str, Any]]:
        self._refresh_node_map()
        rows: list[dict[str, Any]] = []
        for node_name, container in sorted(self._node_containers.items()):
            container.reload()
            image = (
                container.image.tags[0]
                if container.image.tags
                else container.image.short_id
            )
            rows.append(
                {
                    "container_id": container.short_id,
                    "name": node_name,
                    "container_name": container.name.lstrip("/"),
                    "image": image,
                    "status": container.status,
                }
            )
        return rows

    def list_nodes(self) -> list[str]:
        self._refresh_node_map()
        return sorted(self._node_containers.keys())

    def get_container(self, node: str) -> docker.models.containers.Container:
        self._refresh_node_map()
        container = self._node_containers.get(node)
        if container is None:
            raise ValueError(
                f"No container found for node {node!r} in lab {self._lab_name!r}."
            )
        return container

    def exec(self, node: str, cmd: str, *, timeout: float = 10.0) -> str:
        container = self.get_container(node)

        def _run() -> str:
            exit_code, output = container.exec_run(["/bin/sh", "-c", cmd])
            text = (
                output.decode("utf-8", errors="replace")
                if isinstance(output, bytes)
                else str(output)
            )
            if exit_code != 0 and text.strip() == "":
                return f"[exit {exit_code}]"
            return text

        return exec_with_timeout(_run, timeout=timeout, node=node, cmd=cmd)

    def pause(self, node: str) -> None:
        pause_container(self.get_container(node))

    def unpause(self, node: str) -> None:
        unpause_container(self.get_container(node))

    def _build_topology_neighbors(self) -> dict[str, list[str]]:
        spec = parse_clab_topology(self._topology_file)
        neighbors: dict[str, set[str]] = {}
        for link in spec.links:
            left_name, right_name = (
                link.endpoints[0].split(":")[0],
                link.endpoints[1].split(":")[0],
            )
            neighbors.setdefault(left_name, set()).add(right_name)
            neighbors.setdefault(right_name, set()).add(left_name)
        return {name: sorted(peers) for name, peers in neighbors.items()}

    def get_connected_devices(self, node: str) -> list[str]:
        if self._topology_neighbors is None:
            self._topology_neighbors = self._build_topology_neighbors()
        return list(self._topology_neighbors.get(node, []))
