"""Containerlab min 3-node CLOS fabric (clos01) on Nokia SR Linux or SONiC."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
import yaml
from typing import ClassVar

from nika.config import clab_workdir
from nika.net_env.base import ProbePath
from nika.net_env.utils.containerlab.base import ContainerlabNetworkEnv
from nika.runtime.containerlab import render_topology
from nika.runtime.containerlab.parse import parse_clab_topology
from nika.runtime.spec import LabSpec
from nika.net_env.utils.sonic.common import (
    KIND as SONIC_KIND,
    activate_sonic_routers,
    ensure_sonic_image,
    sonic_port,
)
from nika.net_env.utils.sonic.render import (
    SonicRouter,
    clab_kind as sonic_clab_kind,
    translate_frr_design,
    write_node_files as write_sonic_node_files,
)
from nika.utils.dependencies import require_gnmic


class ContainerlabMin3Clos(ContainerlabNetworkEnv):
    ROUTING_PARAMETER_KINDS = frozenset({"bgp_exports"})
    ROUTING_MODELS = frozenset({"srl-bgp"})
    # ref: https://containerlab.dev/lab-examples/min-clos/
    LAB_NAME = "min3clos"
    TOPO_LEVEL = "easy"
    DESC = "3-node CLOS fabric with Nokia SR Linux (Containerlab min-clos / clos01)."
    ROUTERS: ClassVar[tuple[str, ...]] = ("leaf1", "leaf2", "spine")
    GNMI_WAIT_TIMEOUT_SEC: ClassVar[int] = 300

    def __init__(
        self,
        *,
        backend: str = "containerlab",
        routing_model=None,
        routing_parameters=None,
        device_profile: str | None = None,
        **kwargs,
    ):
        from nika.runtime.spec import NodeRole

        super().__init__(backend=backend, **kwargs)
        self.device_profile = device_profile or "nokia_srlinux"
        if self.device_profile not in {"nokia_srlinux", "sonic"}:
            raise ValueError(
                f"min3clos supports device_profile nokia_srlinux or sonic, "
                f"not {self.device_profile!r}"
            )
        self.sonic = self.device_profile == "sonic"
        if self.sonic:
            if routing_model is not None or routing_parameters is not None:
                raise ValueError(
                    "min3clos on SONiC has no routing model for solved parameters"
                )
            self.desc = (
                "3-node CLOS fabric with SONiC (Containerlab min-clos / clos01)."
            )
        self.routing_model = routing_model
        self.routing_documents = None
        if routing_model == "srl-bgp":
            from nika.net_env.synthesis.srl import bgp_sketch, apply_srl_exports

            documents = {
                path.stem: yaml.safe_load(path.read_text())
                for path in (self.lab_dir / "configs").glob("*.yaml")
            }
            self.routing_documents = bgp_sketch(documents, self.bgp_origin_prefixes())
            if routing_parameters is not None:
                self.routing_documents = apply_srl_exports(
                    self.routing_documents, routing_parameters
                )
        elif routing_parameters is not None:
            raise ValueError(
                "SR Linux parameters require the declared srl-bgp routing model"
            )
        for name in self.ROUTERS:
            self.declare_machine(
                name,
                role=NodeRole.ROUTER,
                capabilities=("linux", "sonic" if self.sonic else "srl", "bgp"),
            )
        for name in ("client1", "client2"):
            self.declare_machine(
                name,
                role=NodeRole.HOST,
                capabilities=("linux",),
                reachability_target=True,
            )

    def topology_replacements(self, lab_name: str) -> dict[str, str]:
        from nika.net_env.isp.containerlab.lab import (
            LINUX_CMD,
            LINUX_IMAGE,
            SRL_IMAGE,
        )
        from nika.net_env.utils.containerlab.mgmt_subnet import (
            mgmt_ipv4_address,
            mgmt_ipv4_subnet,
            mgmt_ipv6_address,
            mgmt_ipv6_subnet,
        )

        hosts = (
            ("leaf1", 2),
            ("leaf2", 3),
            ("spine", 4),
            ("client1", 5),
            ("client2", 6),
        )
        replacements = {
            "__MGMT_IPV4_SUBNET__": mgmt_ipv4_subnet(lab_name),
            "__MGMT_IPV6_SUBNET__": mgmt_ipv6_subnet(lab_name),
            "__SRL_IMAGE__": SRL_IMAGE,
            "__LINUX_IMAGE__": LINUX_IMAGE,
            "__LINUX_CMD__": LINUX_CMD,
            "__SRL_ROUTERS__": ""
            if self.sonic
            else " ".join(f'"{name}"' for name in self.ROUTERS),
        }
        for name, index in hosts:
            replacements[f"__MGMT_IP_{name.upper()}__"] = mgmt_ipv4_address(
                lab_name, index
            )
            replacements[f"__MGMT_IPV6_{name.upper()}__"] = mgmt_ipv6_address(
                lab_name, index
            )
        return replacements

    def bgp_origin_prefixes(self):
        # Original client-facing /31 prefixes; no change to node/interface roles.
        return {"10.0.0.24/31": "leaf1", "10.0.0.26/31": "leaf2"}

    def _mgmt_ipv4_for_srl(self) -> dict[str, str]:
        from nika.net_env.utils.containerlab.mgmt_subnet import mgmt_ipv4_address

        lab_name = self.name or self.LAB_NAME
        return {
            name: mgmt_ipv4_address(lab_name, index)
            for name, index in (("leaf1", 2), ("leaf2", 3), ("spine", 4))
        }

    def _prepare_runtime_files(self) -> None:
        lab_name = self.name
        if not lab_name:
            raise ValueError("Lab name is required before deploy.")
        self.runtime_workdir = clab_workdir(lab_name)

        self.topology_file = self.runtime_workdir / f"{self.LAB_NAME}.clab.yml"
        render_topology(
            self.topology_template(),
            lab_name=lab_name,
            output_path=self.topology_file,
            replacements=self.topology_replacements(lab_name),
        )
        if self.sonic:
            self._convert_topology_to_sonic()

        configs_src = self.lab_dir / "configs"
        configs_dst = self.runtime_workdir / "configs"
        if configs_dst.exists():
            shutil.rmtree(configs_dst)
        shutil.copytree(configs_src, configs_dst)
        if self.routing_documents is not None:
            for node, document in self.routing_documents.items():
                (configs_dst / f"{node}.yaml").write_text(
                    yaml.safe_dump(document, sort_keys=False)
                )

        setup_template = self.lab_dir / "setup.sh.tmpl"
        setup_dst = self.runtime_workdir / "setup.sh"
        render_topology(
            setup_template,
            lab_name=lab_name,
            output_path=setup_dst,
            replacements=self.topology_replacements(lab_name),
        )
        setup_dst.chmod(0o755)
        if self.sonic:
            for router in self.sonic_routers.values():
                write_sonic_node_files(configs_dst, router)

    @property
    def sonic_routers(self) -> dict[str, SonicRouter]:
        def rename(iface: str) -> str:
            return "Loopback0" if iface == "lo" else sonic_port(iface)

        return {
            name: translate_frr_design(
                name,
                (self.lab_dir / "configs" / "sonic" / f"{name}.conf").read_text(),
                rename,
            )
            for name in self.ROUTERS
        }

    def _convert_topology_to_sonic(self) -> None:
        document = yaml.safe_load(self.topology_file.read_text())
        self.topology_file.write_text(
            yaml.safe_dump(self._sonic_document(document), sort_keys=False)
        )

    def _sonic_document(self, document: dict) -> dict:
        """Run the template's routers as SONiC, with SR Linux port ``e1-N`` as ``ethN``."""
        topology = document["topology"]
        del topology["kinds"]["nokia_srlinux"]
        topology["kinds"][SONIC_KIND] = sonic_clab_kind()
        for name in self.ROUTERS:
            node = topology["nodes"][name]
            node["kind"] = SONIC_KIND
            node.pop("type", None)
        for link in topology["links"]:
            link["endpoints"] = [
                re.sub(r":e1-(\d+)$", r":eth\1", endpoint)
                for endpoint in link["endpoints"]
            ]
        return document

    def get_lab_spec(self) -> LabSpec:
        if not self.sonic:
            return super().get_lab_spec()
        document = self._sonic_document(
            yaml.safe_load(self.topology_template().read_text())
        )
        with tempfile.NamedTemporaryFile("w", suffix=".clab.yml") as handle:
            yaml.safe_dump(document, handle, sort_keys=False)
            handle.flush()
            spec = parse_clab_topology(handle.name)
        spec.name = self.name or self.LAB_NAME
        return spec

    def _collect_lab_images(self) -> set[str]:
        from nika.net_env.isp.containerlab.lab import LINUX_IMAGE

        return {LINUX_IMAGE}

    def deploy(self) -> None:
        if self.sonic:
            ensure_sonic_image()
        else:
            require_gnmic()
        self._ensure_docker_images()
        already_existed = self.lab_exists()
        super().deploy()
        if already_existed:
            return
        if self.sonic:
            activate_sonic_routers(
                self._build_runtime(),
                {name: r.daemons for name, r in self.sonic_routers.items()},
            )
        else:
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
        pending = set(self._mgmt_ipv4_for_srl().values())
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

    def target_roles(self) -> dict[str, list[str]]:
        roles = super().target_roles()
        clients = [h for h in roles["hosts"] if "client" in h] or roles["hosts"]
        return {
            **roles,
            "hosts": clients,
            "host1_pool": clients,
            "attacker_pool": clients,
            "routers": [r for r in roles["routers"] if "leaf" in r] or roles["routers"],
            "web": clients,
            "l2_endpoints": clients,
        }

    def business_workload(self):
        from nika.net_env.min3clos.workload import build_workload

        if self.sonic:
            # SR Linux port ``e1-N`` runs as container interface ``ethN``.
            return build_workload(lambda port: port.replace("e1-", "eth"))
        return build_workload()

    @classmethod
    def default_probe_path(cls, *, topo_size: str = "s", **deploy_kwargs) -> ProbePath:
        return ProbePath(
            src_host="client1",
            dst_ip="10.0.0.27",
            http_url=None,
            control_plane_host="leaf1",
            peer_host="client2",
        )

    def startup_verify_lab(self) -> dict:
        from nika.net_env.min3clos.verify import verify_min3clos_lab_startup

        return verify_min3clos_lab_startup(
            self._build_runtime(), scenario_name=self.LAB_NAME, sonic=self.sonic
        )

    def verify_lab(self) -> dict:
        from nika.net_env.min3clos.verify import verify_min3clos_lab

        return verify_min3clos_lab(
            self._build_runtime(), scenario_name=self.LAB_NAME, sonic=self.sonic
        )
