"""Kathara-backed LabRuntime implementation."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import nika.runtime.kathara.patch  # noqa: F401
from Kathara.manager.Kathara import Kathara

from nika.runtime.base import LabRuntime
from nika.runtime.shared.containers import pause_container, unpause_container
from nika.runtime.shared.settings import lab_settings as _lab_settings
from nika.runtime.shared.execution import exec_with_timeout, merge_exec_output
from nika.service.shell import ShellResolver
from nika.service.kathara.docker_utils import (
    get_machine_container,
    link_neighbors,
    list_lab_containers,
)
from nika.runtime.spec import MachineInventory

if TYPE_CHECKING:
    from docker.models.containers import Container

    from nika.net_env.base import NetworkEnvBase


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
            # stream=False returns (stdout, stderr, exit_code).
            stdout, stderr, _ = self._instance.exec(
                machine_name=node,
                lab_name=self.lab_name,
                command=cmd,
                stream=False,
            )
            return merge_exec_output(stdout, stderr)

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

    def _machine_containers(self) -> list[Container]:
        return list(self._instance.get_machines_api_objects(lab_name=self.lab_name))

    def _machine_count(self, *, running_only: bool) -> int:
        """Number of this lab's machine containers (optionally only running)."""
        try:
            containers = self._machine_containers()
        except Exception:
            return 0
        if not running_only:
            return len(containers)
        return sum(1 for container in containers if container.status == "running")

    def _link_count(self) -> int:
        """Number of this lab's collision-domain networks still on the host."""
        try:
            return len(self._instance.get_links_api_objects(lab_name=self.lab_name))
        except Exception:
            return 0

    def has_leftover_resources(self) -> bool:
        # Links without machines remain after an interrupted deploy or teardown.
        return self._link_count() > 0

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
            running = self._machine_count(running_only=True)
            if running >= expected:
                return
            time.sleep(2.0)
        raise RuntimeError(
            f"Lab {self.lab_name}: only {running}/{expected} machines running "
            f"after {timeout:.0f}s (raise nika.lab.deploy_ready_timeout_sec "
            "in config/nika.yaml on slow hosts)"
        )

    def deploy(self) -> bool:
        """Deploy the lab, verify readiness, retry transient host failures.

        Returns True when this call deployed the lab, False when it already
        existed. Settling after deploy is the caller's job (see
        ``NetworkEnvBase.deploy``), so it can be skipped when a readiness
        verifier polls anyway.
        """
        # Strict probe: a Docker/Kathara API error must surface here instead
        # of being mistaken for "lab exists" and silently skipping deploy.
        if self._lab_present():
            print(f"Lab {self.lab_name} exists")
            return False
        self._net_env._ensure_docker_images()

        lab = _lab_settings()
        attempts = lab.deploy_attempts
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                Kathara.get_instance().deploy_lab(lab=self._net_env.lab)
                self._wait_deploy_ready(lab.deploy_ready_timeout_sec)
                return True
            except Exception as exc:  # noqa: BLE001 - includes docker APIError
                last_error = exc
                print(
                    f"Deploy of lab {self.lab_name} failed "
                    f"(attempt {attempt}/{attempts}): {exc}"
                )
                # Remove partial containers before the next deployment attempt.
                self.destroy()
                if attempt < attempts:
                    time.sleep(5.0 * attempt)
        raise RuntimeError(
            f"Lab {self.lab_name} failed to deploy after {attempts} attempts"
        ) from last_error

    def destroy(self) -> None:
        """Undeploy the lab and VERIFY its containers are gone."""
        # Dynamic fault proxies are intentionally outside the Kathara lab
        # inventory, so remove only resources explicitly labelled for this lab.
        try:
            from nika.runtime.kathara.vde_proxy import KatharaVdeFaultProxy

            KatharaVdeFaultProxy.cleanup_lab(self.lab_name)
        except Exception as exc:  # cleanup must not prevent normal teardown
            print(f"Error cleaning VDE fault proxies: {exc}")
        try:
            self._instance.undeploy_lab(lab_name=self.lab_name)
        except Exception as exc:
            print(f"Error undeploying lab {self.lab_name}: {exc}")

        deadline = time.monotonic() + _lab_settings().undeploy_verify_timeout_sec
        retried = False
        while time.monotonic() < deadline:
            machines = self._machine_count(running_only=False)
            # undeploy_lab removes links only after every machine call succeeds;
            # a failed machine removal leaves collision domains behind.
            if machines == 0 and self._link_count() == 0:
                return
            if not retried or machines == 0:
                # one forced second attempt before we give up
                retried = True
                try:
                    self._instance.undeploy_lab(lab_name=self.lab_name)
                except Exception as exc:
                    print(f"Error re-undeploying lab {self.lab_name}: {exc}")
            time.sleep(2.0)
        # `kathara wipe` would delete every lab on the host, including other
        # running sessions; point only at this lab's owning session.
        print(
            f"WARNING: lab {self.lab_name} still has "
            f"{self._machine_count(running_only=False)} container(s) and "
            f"{self._link_count()} collision domain(s) after undeploy. It is "
            "leaked and keeps consuming resources. Clean it up with "
            "`nika session close <session_id>` for the session that owns it."
        )

    def _lab_present(self) -> bool:
        """Return whether the lab has machines; raise on API errors."""
        try:
            tmp_lab = self._instance.get_lab_from_api(lab_name=self.lab_name)
        except KeyError:
            # A dynamically inserted controller-only VDE proxy replaces a lab
            # network, and Kathara's live parser then raises KeyError. The
            # lab is deployed in that case.
            return True
        if tmp_lab is None:
            return False
        return bool(tmp_lab.machines)

    def exists(self) -> bool:
        try:
            return self._lab_present()
        except Exception:
            # Keep teardown reachable when the inventory is unreadable:
            # ``destroy()`` removes only resources labelled for this lab.
            return True

    def inspect(self) -> list[dict[str, Any]]:
        return list_lab_containers(lab_name=self.lab_name)

    def list_nodes(self) -> list[str]:
        """Return machines that have a container now (live, not the lab file)."""
        names = {
            (container.labels or {}).get("name")
            for container in self._machine_containers()
        }
        return sorted(name for name in names if name)

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
        return link_neighbors(links.values(), node)

    def list_dhcp_client_nodes(self) -> list[str]:
        """Return nodes explicitly declared as DHCP clients."""
        inventory = MachineInventory(self._net_env.machine_identities)
        if self._net_env.lab and self._net_env.lab.machines:
            inventory.validate(set(self._net_env.lab.machines))
        return inventory.names_for_capability("dhcp_client")
