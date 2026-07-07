"""Kathara-backed LabRuntime implementation."""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

import nika.runtime.kathara.patch  # noqa: F401
from Kathara.manager.Kathara import Kathara

from nika.runtime.base import LabRuntime
from nika.runtime.docker_ops import pause_container, unpause_container
from nika.runtime.exec_utils import exec_with_timeout
from nika.service.shell import ShellResolver
from nika.service.kathara.docker_utils import get_machine_container, list_lab_containers

if TYPE_CHECKING:
    from docker.models.containers import Container

    from nika.net_env.base import NetworkEnvBase

# Lab lifecycle robustness knobs (env-overridable).
# Deploy: transient host failures (e.g. Docker/systemd cgroup timeouts under
# container churn) are retried after cleaning the partial deploy; readiness is
# verified by polling instead of hoping a fixed sleep was long enough.
DEPLOY_ATTEMPTS = int(os.getenv("NIKA_DEPLOY_ATTEMPTS", "3"))
DEPLOY_READY_TIMEOUT_SEC = float(os.getenv("NIKA_DEPLOY_READY_TIMEOUT", "90"))
DEPLOY_SETTLE_SEC = float(os.getenv("NIKA_DEPLOY_SETTLE", "5"))
# Undeploy: verify the lab's containers are actually gone (a silently leaked
# lab keeps burning CPU and skews later runs).
UNDEPLOY_VERIFY_TIMEOUT_SEC = float(os.getenv("NIKA_UNDEPLOY_VERIFY_TIMEOUT", "30"))


class KatharaRuntime(LabRuntime):
    """Wrap existing Kathara deploy/exec behavior without changing semantics."""

    def __init__(self, net_env: NetworkEnvBase) -> None:
        self._net_env = net_env
        self._instance = net_env.instance or Kathara.get_instance()
        self._shell = ShellResolver()

    @property
    def backend(self) -> str:
        return "kathara"

    def _exec_raw(self, node: str, cmd: str, *, timeout: float = 10.0) -> str:
        def _run() -> str:
            output_generator = self._instance.exec(
                machine_name=node,
                lab_name=self.lab_name,
                command=cmd,
                stream=False,
            )
            chunks: list[str] = []
            for item in output_generator:
                if (
                    not item
                    or item == b""
                    or isinstance(item, int)
                    or item is None
                    or item == "None"
                ):
                    continue
                if isinstance(item, bytes):
                    chunks.append(item.decode("utf-8", errors="ignore"))
                elif isinstance(item, str):
                    chunks.append(item)
                else:
                    chunks.append(str(item))
            return "".join(chunks).strip()

        return exec_with_timeout(_run, timeout=timeout, node=node, cmd=cmd)

    def _preferred_shell(self, node: str) -> str | None:
        lab = self._net_env.lab
        if lab is None:
            return None
        machine = lab.machines.get(node)
        if machine is None or "shell" not in machine.meta:
            return None
        return machine.get_shell()

    @property
    def lab_name(self) -> str:
        return self._net_env.name or self._net_env.lab.name

    def _running_machine_count(self) -> int:
        """Number of this lab's machines with a running container."""
        try:
            lab = self._instance.get_lab_from_api(lab_name=self.lab_name)
            if lab is None or lab.machines is None:
                return 0
            return len(lab.machines)
        except Exception:
            return 0

    def _wait_deploy_ready(self, timeout: float) -> None:
        """Poll until every expected machine has a running container.

        Replaces hoping that a fixed sleep was long enough: on a loaded host
        containers can take far longer than 5s to come up, and tools that
        exec into a machine before it exists fail in confusing ways.
        """
        lab = self._net_env.lab
        expected = len(lab.machines) if lab and lab.machines else 0
        if expected == 0:
            return
        deadline = time.monotonic() + timeout
        running = 0
        while time.monotonic() < deadline:
            running = self._running_machine_count()
            if running >= expected:
                return
            time.sleep(2.0)
        raise RuntimeError(
            f"Lab {self.lab_name}: only {running}/{expected} machines running "
            f"after {timeout:.0f}s (raise NIKA_DEPLOY_READY_TIMEOUT on slow hosts)"
        )

    def deploy(self) -> None:
        """Deploy the lab, verify readiness, retry transient host failures."""
        if self.exists():
            print(f"Lab {self.lab_name} exists")
            return
        self._net_env._ensure_docker_images()

        last_error: Exception | None = None
        for attempt in range(1, DEPLOY_ATTEMPTS + 1):
            try:
                Kathara.get_instance().deploy_lab(lab=self._net_env.lab)
                self._wait_deploy_ready(DEPLOY_READY_TIMEOUT_SEC)
                # short settle so services inside the containers can boot
                time.sleep(DEPLOY_SETTLE_SEC)
                return
            except Exception as exc:  # noqa: BLE001 - includes docker APIError
                last_error = exc
                print(
                    f"Deploy of lab {self.lab_name} failed "
                    f"(attempt {attempt}/{DEPLOY_ATTEMPTS}): {exc}"
                )
                # Clean the partial deploy before retrying, or the retry
                # collides with half-started containers.
                self.destroy()
                if attempt < DEPLOY_ATTEMPTS:
                    time.sleep(5.0 * attempt)
        raise RuntimeError(
            f"Lab {self.lab_name} failed to deploy after {DEPLOY_ATTEMPTS} attempts"
        ) from last_error

    def destroy(self) -> None:
        """Undeploy the lab and VERIFY its containers are gone."""
        try:
            self._instance.undeploy_lab(lab_name=self.lab_name)
        except Exception as exc:
            print(f"Error undeploying lab {self.lab_name}: {exc}")

        deadline = time.monotonic() + UNDEPLOY_VERIFY_TIMEOUT_SEC
        retried = False
        while time.monotonic() < deadline:
            leftover = self._running_machine_count()
            if leftover == 0:
                return
            if not retried:
                # one forced second attempt before we give up
                retried = True
                try:
                    self._instance.undeploy_lab(lab_name=self.lab_name)
                except Exception as exc:
                    print(f"Error re-undeploying lab {self.lab_name}: {exc}")
            time.sleep(2.0)
        print(
            f"WARNING: lab {self.lab_name} still has {self._running_machine_count()} "
            "container(s) after undeploy — it is LEAKED and keeps consuming "
            "resources. Clean up with `nika session close`/`kathara wipe`."
        )

    def exists(self) -> bool:
        tmp_lab = self._instance.get_lab_from_api(lab_name=self.lab_name)
        if tmp_lab is None:
            return False
        tmp_machines = tmp_lab.machines
        if tmp_machines is None or len(tmp_machines) == 0:
            return False
        return True

    def inspect(self) -> list[dict[str, Any]]:
        return list_lab_containers(lab_name=self.lab_name)

    def list_nodes(self) -> list[str]:
        if self._net_env.lab and self._net_env.lab.machines:
            return sorted(self._net_env.lab.machines.keys())
        tmp_lab = self._instance.get_lab_from_api(lab_name=self.lab_name)
        if tmp_lab is None:
            return []
        return sorted(tmp_lab.machines.keys())

    def exec(self, node: str, cmd: str, *, timeout: float = 10.0) -> str:
        return self._shell.exec_via_shell(
            node,
            cmd,
            self._exec_raw,
            preferred_shell=self._preferred_shell(node),
            timeout=timeout,
        )

    def get_container(self, node: str) -> Container:
        return get_machine_container(lab_name=self.lab_name, host_name=node)

    def pause(self, node: str) -> None:
        pause_container(self.get_container(node))

    def unpause(self, node: str) -> None:
        unpause_container(self.get_container(node))

    def get_connected_devices(self, node: str) -> list[str]:
        links = next(self._instance.get_links_stats(lab_name=self.lab_name))
        results: list[str] = []
        for link in links.values():
            if not link.name:
                continue
            left = link.containers[0].labels["name"]
            right = link.containers[1].labels["name"]
            if node == left:
                results.append(right)
            elif node == right:
                results.append(left)
        return results

    def list_dhcp_client_nodes(self) -> list[str]:
        """Return lab nodes that typically receive DHCP leases."""
        nodes: list[str] = []
        if not self._net_env.lab or not self._net_env.lab.machines:
            return self.list_nodes()
        for name, machine in self._net_env.lab.machines.items():
            image = machine.get_image()
            if "base" in image and any(key in name for key in ("pc", "client")):
                nodes.append(name)
        return nodes
