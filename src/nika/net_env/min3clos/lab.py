"""Containerlab min 3-node CLOS fabric (clos01)."""

from __future__ import annotations

import shutil
import subprocess
import time
from typing import ClassVar

from nika.net_env.utils.containerlab.base import ContainerlabNetworkEnv
from nika.runtime.containerlab import render_topology

_SRL_MGMT_IPV4: dict[str, str] = {
    "leaf1": "172.100.100.2",
    "leaf2": "172.100.100.3",
    "spine": "172.100.100.4",
}


class ContainerlabMin3Clos(ContainerlabNetworkEnv):
    # ref: https://containerlab.dev/lab-examples/min-clos/
    LAB_NAME = "min3clos"
    TOPO_LEVEL = "easy"
    TOPO_SIZE = 5
    TAGS = ["clos", "srl", "bgp", "link", "containerlab", "fabric"]
    DESC = "3-node CLOS fabric with Nokia SR Linux (Containerlab min-clos / clos01)."
    GNMI_WAIT_TIMEOUT_SEC: ClassVar[int] = 300

    def __init__(self, *, backend: str = "containerlab", **kwargs):
        from nika.runtime.spec import NodeRole

        super().__init__(backend=backend, **kwargs)
        for name in ("leaf1", "leaf2", "spine"):
            self.declare_machine(
                name,
                role=NodeRole.ROUTER,
                capabilities=("linux", "srl", "bgp"),
            )
        for name in ("client1", "client2"):
            self.declare_machine(
                name,
                role=NodeRole.HOST,
                capabilities=("linux",),
                reachability_target=True,
            )

    def _prepare_runtime_files(self) -> None:
        super()._prepare_runtime_files()
        lab_name = self.name
        if not lab_name or self.runtime_workdir is None:
            raise ValueError("Lab name is required before deploy.")

        configs_src = self.lab_dir / "configs"
        configs_dst = self.runtime_workdir / "configs"
        if configs_dst.exists():
            shutil.rmtree(configs_dst)
        shutil.copytree(configs_src, configs_dst)

        setup_template = self.lab_dir / "setup.sh.tmpl"
        setup_dst = self.runtime_workdir / "setup.sh"
        render_topology(setup_template, lab_name=lab_name, output_path=setup_dst)
        setup_dst.chmod(0o755)

    def deploy(self) -> None:
        already_existed = self.lab_exists()
        super().deploy()
        if already_existed:
            return
        self._wait_for_gnmi()
        self._run_setup()

    def _gnmi_ready(self, mgmt_ipv4: str) -> bool:
        result = subprocess.run(
            [
                "gnmic",
                "-a",
                f"{mgmt_ipv4}:57400",
                "--timeout",
                "5s",
                "-u",
                "admin",
                "-p",
                "NokiaSrl1!",
                "-e",
                "json_ietf",
                "--skip-verify",
                "get",
                "--path",
                "/system/name/host-name",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0

    def _wait_for_gnmi(self) -> None:
        pending = set(_SRL_MGMT_IPV4.values())
        deadline = time.time() + self.GNMI_WAIT_TIMEOUT_SEC
        while time.time() < deadline and pending:
            for addr in list(pending):
                if self._gnmi_ready(addr):
                    pending.discard(addr)
            if pending:
                time.sleep(5)
        if pending:
            raise RuntimeError(
                f"gNMI not ready within {self.GNMI_WAIT_TIMEOUT_SEC}s on: {sorted(pending)}"
            )

    def _run_setup(self) -> None:
        self._ensure_runtime_files()
        if self.runtime_workdir is None:
            raise ValueError("runtime_workdir is required for setup.")
        setup_script = self.runtime_workdir / "setup.sh"
        if not setup_script.is_file():
            raise FileNotFoundError(f"Missing setup script: {setup_script}")
        result = subprocess.run(
            ["bash", str(setup_script)],
            cwd=str(self.runtime_workdir),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"min3clos setup.sh failed: {result.stderr or result.stdout}"
            )

    def startup_verify_lab(self) -> dict:
        from nika.net_env.min3clos.verify import verify_min3clos_lab_startup

        return verify_min3clos_lab_startup(
            self._build_runtime(), scenario_name=self.LAB_NAME
        )

    def verify_lab(self) -> dict:
        from nika.net_env.min3clos.verify import verify_min3clos_lab

        return verify_min3clos_lab(self._build_runtime(), scenario_name=self.LAB_NAME)
