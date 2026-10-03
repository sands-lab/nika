"""Containerlab-backed LabRuntime implementation."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import docker

from nika.runtime.base import LabRuntime
from nika.runtime.containerlab.parse import parse_clab_topology
from nika.runtime.shared.containers import (
    docker_client,
    pause_container,
    unpause_container,
)
from nika.runtime.shared.execution import (
    exec_with_timeout,
    merge_exec_output,
    without_shell_history,
)
from nika.runtime.shared.settings import lab_settings as _lab_settings

# Upper bounds for ``clab`` subprocesses. Large SR Linux / XRd labs take
# minutes to deploy; a hung clab must still fail instead of blocking forever.
_CLAB_TIMEOUT_SEC = {"deploy": 900.0, "destroy": 300.0, "inspect": 60.0}
_CLAB_DEFAULT_TIMEOUT_SEC = 60.0
_CLAB_TIMEOUT_RETURNCODE = 124


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
        # Logical node -> container. Filled from ``clab inspect`` and reused
        # by exec/get_container; refreshed on a miss or a Docker NotFound.
        self._node_containers: dict[str, docker.models.containers.Container] = {}
        self._node_map_lock = threading.Lock()
        self._topology_neighbors: dict[str, list[str]] | None = None

    @property
    def backend(self) -> str:
        return "containerlab"

    @property
    def _docker(self) -> docker.DockerClient:
        # Lazy: topology-only use (e.g. neighbor lookup) needs no daemon.
        return docker_client()

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
        timeout = _CLAB_TIMEOUT_SEC.get(
            args[0] if args else "", _CLAB_DEFAULT_TIMEOUT_SEC
        )
        try:
            return subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                cwd=str(self._runtime_workdir),
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            # TimeoutExpired carries bytes even with text=True.
            partial = exc.stdout or b""
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", errors="replace")
            return subprocess.CompletedProcess(
                cmd,
                _CLAB_TIMEOUT_RETURNCODE,
                stdout=partial,
                stderr=f"clab {' '.join(args[:1])} timed out after {timeout:.0f}s",
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

    def _refresh_node_map(self) -> bool:
        """Reload the node map from ``clab inspect``.

        Returns False when inspect itself failed. The previous map is kept
        then: a transient inspect failure (e.g. under parallel verification)
        must not make every concurrent exec report a missing node.
        """
        with self._node_map_lock:
            mapping = self._inspect_node_map()
            if mapping is None:
                return False
            self._node_containers = mapping
            return True

    def _inspect_node_map(
        self,
    ) -> dict[str, docker.models.containers.Container] | None:
        result = self._run_clab(
            "inspect",
            "-t",
            str(self._topology_file),
            "--format",
            "json",
        )
        if result.returncode != 0:
            return None
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
            try:
                mapping[node_name] = self._docker.containers.get(container_id)
            except docker.errors.NotFound:
                continue
        return mapping

    def deploy(self) -> bool:
        """Deploy via ``clab``; return False when the lab already exists."""
        if self.exists():
            print(f"Lab {self._lab_name} exists")
            return False
        last_error = ""
        max_workers = str(_lab_settings().containerlab_max_workers)
        for attempt in range(1, 3):
            # Cap concurrent create/wire workers so many SRL nodes do not
            # spike host RAM during deploy (steady-state use is usually lower).
            result = self._run_clab(
                "deploy",
                "-t",
                str(self._topology_file),
                "--reconfigure",
                "--max-workers",
                max_workers,
            )
            if result.returncode == 0:
                self._refresh_node_map()
                return True
            err = (result.stderr or result.stdout or "").strip()
            last_error = err
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
            # SRL mgmt gRPC can reject keepalives under parallel post-deploy load.
            if attempt < 2 and ("too_many_pings" in low or "enhance_your_calm" in low):
                try:
                    self._run_clab(
                        "destroy",
                        "-t",
                        str(self._topology_file),
                        "--cleanup",
                    )
                except Exception:  # noqa: BLE001 - best-effort before retry
                    pass
                time.sleep(5)
                continue
            break
        raise RuntimeError(f"clab deploy failed for {self._lab_name}: {last_error}")

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
        with self._node_map_lock:
            self._node_containers = {}

    def exists(self) -> bool:
        return self._refresh_node_map() and bool(self._node_containers)

    def inspect(self) -> list[dict[str, Any]]:
        if not self._refresh_node_map():
            return []
        rows: list[dict[str, Any]] = []
        for node_name, container in sorted(self._node_containers.items()):
            try:
                container.reload()
            except docker.errors.NotFound:
                continue
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
        if not self._refresh_node_map():
            return []
        return sorted(self._node_containers.keys())

    def _cached_container(self, node: str) -> docker.models.containers.Container:
        """Return the mapped container, running ``clab inspect`` only on a miss."""
        container = self._node_containers.get(node)
        if container is None:
            self._refresh_node_map()
            container = self._node_containers.get(node)
        if container is None:
            raise ValueError(
                f"No container found for node {node!r} in lab {self._lab_name!r}."
            )
        return container

    def get_container(self, node: str) -> docker.models.containers.Container:
        """Return ``node``'s container with fresh state (status, PID)."""
        container = self._cached_container(node)
        try:
            container.reload()
        except docker.errors.NotFound:
            self._forget_container(node, container)
            container = self._cached_container(node)
            container.reload()
        return container

    def _forget_container(self, node: str, container: Any) -> None:
        with self._node_map_lock:
            if self._node_containers.get(node) is container:
                self._node_containers = {
                    name: item
                    for name, item in self._node_containers.items()
                    if name != node
                }

    def exec(self, node: str, cmd: str, *, timeout: float = 10.0) -> str:
        container = self._cached_container(node)
        shell_cmd = without_shell_history(cmd)

        def _run() -> str:
            try:
                _, (stdout, stderr) = container.exec_run(
                    ["/bin/sh", "-c", shell_cmd], demux=True
                )
            except docker.errors.NotFound:
                # The lab was redeployed under the same name; look it up again.
                self._forget_container(node, container)
                _, (stdout, stderr) = self._cached_container(node).exec_run(
                    ["/bin/sh", "-c", shell_cmd], demux=True
                )
            return merge_exec_output(stdout, stderr)

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
