"""Containerlab ISP scenario compiled from SNDlib topologies (Nokia SR Linux)."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import ClassVar, Literal

import yaml

from nika.config import RUNTIME_DIR
from nika.net_env.utils.containerlab.base import ContainerlabNetworkEnv
from nika.net_env.isp.bgp import (
    DEFAULT_BGP_MODE,
    BgpPlan,
    IspBgpMode,
    compile_bgp_plan,
    normalize_bgp_mode,
    scope_igp_to_bgp_as,
)
from nika.net_env.isp.bgp.srl import render_bgp_srl_block
from nika.net_env.isp.igp import (
    DEFAULT_CONSTANT_METRIC,
    DEFAULT_IGP,
    DEFAULT_METRIC_STRATEGY,
    IspConfig,
    IspPlan,
    compile_isp_plan,
)
from nika.net_env.utils.containerlab.mgmt_subnet import (
    mgmt_ipv4_address,
    mgmt_ipv4_subnet,
)
from nika.net_env.isp.igp.ifaces import srl_e1_name
from nika.net_env.isp.igp.srl import render_srl_node_config
from nika.net_env.isp.profiles import (
    default_device_profile,
    normalize_device_profile,
    validate_backend_profile,
)
from nika.net_env.isp.traffic import (
    IspTrafficAttachment,
    TrafficInterval,
    TrafficMatrixSeries,
    attach_traffic_stubs,
    remap_inventory_ifaces_to_srl,
)
from nika.runtime.containerlab.parse import parse_clab_topology
from nika.runtime.spec import LabSpec

IgpLiteral = Literal["isis", "ospf"]
MetricLiteral = Literal["constant", "routing_cost", "inv_capacity"]

SRL_IMAGE = "ghcr.io/nokia/srlinux:24.10"
SRL_TYPE = "ixr-d2l"
LINUX_IMAGE = "ghcr.io/hellt/network-multitool"
SRL_PASSWORD = "NokiaSrl1!"


def _stub_series_all_routers(plan: IspPlan) -> TrafficMatrixSeries:
    return TrafficMatrixSeries(
        topology=plan.topology_name,
        source="demands",
        intervals=(TrafficInterval(index=0, duration_sec=5, flows=()),),
        sample_period_sec=5,
        unit_note="stub-layout only",
        path=None,
    )


class Isp(ContainerlabNetworkEnv):
    """SNDlib ISP on Containerlab with nokia_srlinux routers + linux stubs."""

    LAB_NAME = "isp"
    TOPO_LEVEL = "medium"
    TOPO_SIZE = None
    TAGS = [
        "isp",
        "sndlib",
        "srl",
        "isis",
        "ospf",
        "bgp",
        "igp",
        "link",
        "icmp",
        "containerlab",
    ]
    DESC = "ISP from SNDlib on Containerlab (Nokia SR Linux)."
    GNMI_WAIT_TIMEOUT_SEC: ClassVar[int] = 600
    SUPPORTED_BACKENDS: ClassVar[list[str]] = ["containerlab"]

    def __init__(
        self,
        topo: str | Path | None = None,
        igp: IgpLiteral = DEFAULT_IGP,
        metric_strategy: MetricLiteral = DEFAULT_METRIC_STRATEGY,
        constant_metric: int = DEFAULT_CONSTANT_METRIC,
        bgp_mode: IspBgpMode | str = DEFAULT_BGP_MODE,
        rpki: bool = False,
        device_profile: str | None = None,
        topo_size: str | None = None,
        scenario_id: str | None = None,
        **kwargs,
    ):
        kwargs.pop("traffic_mode", None)
        kwargs.pop("traffic_scale", None)
        kwargs.pop("rtbh", None)
        super().__init__(backend=kwargs.pop("backend", "containerlab"), **kwargs)

        if topo_size is not None:
            raise ValueError(
                "ISP topology identity is the scenario name "
                "(e.g. isp_abilene); do not pass topo_size."
            )
        if topo is None:
            raise ValueError(
                "ISP requires an explicit topo (supplied by scenario deploy_defaults)."
            )
        self.topo = topo
        if scenario_id is not None:
            self.scenario_id = scenario_id
        elif isinstance(topo, str) and not str(topo).endswith(".xml"):
            self.scenario_id = f"isp_{topo}"
        else:
            raise ValueError(
                "ISP requires scenario_id when topo is a path/XML file "
                "(e.g. scenario_id='isp_abilene')."
            )
        self.igp = igp
        self.metric_strategy = metric_strategy
        self.constant_metric = constant_metric
        raw_mode = bgp_mode if isinstance(bgp_mode, str) else bgp_mode
        self.rpki = bool(rpki)
        self.bgp_mode: IspBgpMode = normalize_bgp_mode(
            raw_mode if isinstance(raw_mode, str) else raw_mode
        )
        if self.rpki:
            raise ValueError(
                "RPKI capability is Kathara/FRR-only; use a named RPKI scenario "
                "with --backend kathara (e.g. isp_abilene_ebgp_rpki)."
            )
        profile_raw = device_profile or default_device_profile("containerlab")
        self.device_profile = normalize_device_profile(profile_raw)
        validate_backend_profile("containerlab", self.device_profile)

        config = IspConfig(
            topology=self.topo,
            igp=igp,
            metric_strategy=metric_strategy,
            constant_metric=constant_metric,
        )
        base_plan = compile_isp_plan(config)
        self.bgp_plan: BgpPlan | None = compile_bgp_plan(base_plan, self.bgp_mode)
        base_plan = scope_igp_to_bgp_as(base_plan, self.bgp_plan)
        stub_series = _stub_series_all_routers(base_plan)
        attachment = attach_traffic_stubs(
            base_plan,
            stub_series,
            scale=1.0,
            pop_node_ids=tuple(n.node_id for n in base_plan.nodes),
            host_iface="eth1",
            render_frr=False,
        )
        self.traffic: IspTrafficAttachment = remap_inventory_ifaces_to_srl(attachment)
        self.plan: IspPlan = self.traffic.plan
        self.inventory = dict(self.plan.inventory)
        if self.bgp_plan is not None:
            self.inventory["bgp"] = self.bgp_plan.inventory
        self.inventory["device_profile"] = self.device_profile
        self.inventory["backend"] = "containerlab"

        self.name = self.scenario_id

        node_n = len(self.plan.nodes)
        link_n = len(self.plan.links)
        host_n = len(self.traffic.hosts)
        wait = max(300, min(3600, 120 + node_n * 20 + link_n + host_n * 5))
        if self.bgp_plan is not None:
            wait = max(wait, min(3600, 180 + node_n * 25 + link_n))
        self.VERIFY_MAX_WAIT_SEC = wait
        self.VERIFY_RETRY_DELAY_SEC = 10.0

        bgp_desc = (
            f"BGP={self.bgp_mode}."
            if self.bgp_plan is not None
            else "Infrastructure loopbacks only; BGP disabled."
        )
        self.desc = (
            f"ISP from SNDlib topology '{self.plan.topology_name}' on Containerlab "
            f"({len(self.plan.nodes)} SRL routers, {len(self.plan.links)} links, "
            f"{host_n} edge stubs, IGP={self.plan.igp}, "
            f"metric={self.plan.metric_strategy}, "
            f"device_profile={self.device_profile}). {bgp_desc}"
        )
        self._populate_role_lists()

    def _assign_mgmt_ips(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        index = 2
        lab = self.name or self.scenario_id
        for node in sorted(self.plan.nodes, key=lambda n: n.device_name):
            mapping[node.device_name] = mgmt_ipv4_address(lab, index)
            index += 1
        for host in sorted(self.traffic.hosts, key=lambda h: h.host_name):
            mapping[host.host_name] = mgmt_ipv4_address(lab, index)
            index += 1
            if index > 254:
                raise ValueError("Too many ISP nodes for mgmt /24.")
        return mapping

    def _populate_role_lists(self) -> None:
        self.routers = sorted(n.device_name for n in self.plan.nodes)
        self.hosts = sorted(h.host_name for h in self.traffic.hosts)
        self.switches = []
        self.bmv2_switches = []
        self.ovs_switches = []
        self.sdn_controllers = []
        self.servers = {}

    def _build_clab_document(self, *, lab_name: str) -> dict:
        if not getattr(self, "_mgmt_ipv4", None):
            self._mgmt_ipv4 = self._assign_mgmt_ips()
        mgmt_subnet = mgmt_ipv4_subnet(lab_name)
        nodes: dict = {}
        for node in self.plan.nodes:
            nodes[node.device_name] = {
                "kind": "nokia_srlinux",
                "type": SRL_TYPE,
                "mgmt-ipv4": self._mgmt_ipv4[node.device_name],
            }
        for host in self.traffic.hosts:
            nodes[host.host_name] = {
                "kind": "linux",
                "image": LINUX_IMAGE,
                "mgmt-ipv4": self._mgmt_ipv4[host.host_name],
            }

        links: list[dict] = []
        for link in self.plan.links:
            links.append(
                {
                    "endpoints": [
                        f"{link.endpoint_a}:{srl_e1_name(link.iface_a)}",
                        f"{link.endpoint_b}:{srl_e1_name(link.iface_b)}",
                    ]
                }
            )
        for edge in self.traffic.edge_links:
            router = next(
                n for n in self.plan.nodes if n.device_name == edge.router_device
            )
            eth_name = next(
                i.name
                for i in router.interfaces
                if i.passive and i.peer_device == edge.host_name
            )
            links.append(
                {
                    "endpoints": [
                        f"{edge.router_device}:{srl_e1_name(eth_name)}",
                        f"{edge.host_name}:eth1",
                    ]
                }
            )
        return {
            "name": lab_name,
            "mgmt": {
                "network": f"br-{lab_name}",
                "ipv4-subnet": mgmt_subnet,
            },
            "topology": {
                "kinds": {
                    "nokia_srlinux": {"image": SRL_IMAGE},
                    "linux": {"image": LINUX_IMAGE},
                },
                "nodes": nodes,
                "links": links,
            },
        }

    def topology_template(self) -> Path:
        # Dynamic scenario: generated under runtime_workdir.
        if self.topology_file is not None:
            return self.topology_file
        return self.lab_dir / "isp.clab.yml"

    def _prepare_runtime_files(self) -> None:
        lab_name = self.name
        if not lab_name:
            raise ValueError("Lab name is required before deploy.")
        self.runtime_workdir = RUNTIME_DIR / "containerlab" / lab_name
        self.runtime_workdir.mkdir(parents=True, exist_ok=True)
        self.topology_file = self.runtime_workdir / f"{self.scenario_id}.clab.yml"

        self._mgmt_ipv4 = self._assign_mgmt_ips()
        doc = self._build_clab_document(lab_name=lab_name)
        self.topology_file.write_text(
            yaml.safe_dump(doc, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
        self._write_configs()
        self._write_setup_script(lab_name=lab_name)

    def _write_configs(self) -> None:
        assert self.runtime_workdir is not None
        configs = self.runtime_workdir / "configs"
        configs.mkdir(parents=True, exist_ok=True)

        bgp_by_device = {}
        if self.bgp_plan is not None:
            bgp_by_device = {n.device_name: n for n in self.bgp_plan.nodes}

        for node in self.plan.nodes:
            bgp_node = bgp_by_device.get(node.device_name)
            bgp_block = None
            extras: list[str] = []
            include_policy = False
            if bgp_node is not None and self.bgp_plan is not None:
                bgp_block = render_bgp_srl_block(bgp_node, self.bgp_plan)
                include_policy = True
                for pref in bgp_node.originated:
                    plen = pref.prefix.rsplit("/", 1)[1]
                    extras.append(f"{pref.ping_address}/{plen}")
            yaml_text = render_srl_node_config(
                node,
                igp=self.plan.igp,
                interfaces=node.interfaces,
                bgp_block=bgp_block,
                extra_loopback_addrs=tuple(extras),
                include_routing_policy=include_policy,
            )
            (configs / f"{node.device_name}.yaml").write_text(
                yaml_text, encoding="utf-8"
            )

        for host in self.traffic.hosts:
            script = "\n".join(
                ["#!/bin/bash", "set -euo pipefail", *host.startup_commands, ""]
            )
            path = configs / f"{host.host_name}.sh"
            path.write_text(script, encoding="utf-8")
            path.chmod(0o755)

    def _write_setup_script(self, *, lab_name: str) -> None:
        assert self.runtime_workdir is not None
        routers = sorted(n.device_name for n in self.plan.nodes)
        clients = sorted(h.host_name for h in self.traffic.hosts)
        mgmt_lines = "\n".join(
            f"  [{name}]={self._mgmt_ipv4[name]}" for name in [*routers, *clients]
        )
        ne_list = " ".join(f'"{r}"' for r in routers)
        client_list = " ".join(f'"{c}"' for c in clients)
        script = f"""#!/bin/bash
set -euo pipefail

CFG_DIR=./configs
SRL_PASSWORD={SRL_PASSWORD}
FAILED=0

declare -A MGMT_IP=(
{mgmt_lines}
)

configure_SRL() {{
  local node=$1
  local out
  out=$(gnmic -a "${{MGMT_IP[$node]}}:57400" --timeout 30s -u admin -p "$SRL_PASSWORD" -e json_ietf --skip-verify set --update-path / --update-file "$CFG_DIR/$node.yaml" 2>&1) || true
  if echo "$out" | grep -q -e '"operation": "UPDATE"'; then
    docker exec "clab-{lab_name}-$node" sr_cli "save startup" > /dev/null
    return 0
  fi
  echo "Error: Unable to push config into clab-{lab_name}-$node."
  echo "$out" >&2
  return 1
}}

configure_CLIENT() {{
  docker cp "$CFG_DIR/$1.sh" "clab-{lab_name}-$1:/tmp/" || return 1
  docker exec "clab-{lab_name}-$1" bash "/tmp/$1.sh" || return 1
}}

echo
NE=({ne_list})
CLIENT=({client_list})

for VARIANT in "${{NE[@]}}"; do
  echo "Configuring $VARIANT..."
  if configure_SRL "$VARIANT"; then
    echo "Configured $VARIANT success"
  else
    echo "Configured $VARIANT fail"
    FAILED=1
  fi
done

for VARIANT in "${{CLIENT[@]}}"; do
  echo "Configuring $VARIANT..."
  if configure_CLIENT "$VARIANT"; then
    echo "Configured $VARIANT success"
  else
    echo "Configured $VARIANT fail"
    FAILED=1
  fi
done
echo

exit "$FAILED"
"""
        setup = self.runtime_workdir / "setup.sh"
        setup.write_text(script, encoding="utf-8")
        setup.chmod(0o755)

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
                SRL_PASSWORD,
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
        pending = {self._mgmt_ipv4[n.device_name] for n in self.plan.nodes}
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
        log_path = self.runtime_workdir / "setup.log"
        with log_path.open("w", encoding="utf-8") as log_file:
            result = subprocess.run(
                ["bash", str(setup_script)],
                cwd=str(self.runtime_workdir),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        if result.returncode != 0:
            tail = ""
            if log_path.is_file():
                text = log_path.read_text(encoding="utf-8", errors="replace")
                tail = text[-4000:] if len(text) > 4000 else text
            raise RuntimeError(
                f"isp setup.sh failed (exit {result.returncode}); "
                f"log={log_path}\n{tail}"
            )

    def get_lab_spec(self) -> LabSpec:
        if self.topology_file is not None and self.topology_file.is_file():
            spec = parse_clab_topology(self.topology_file)
        else:
            # Pre-deploy: synthesize from in-memory doc.
            tmp = yaml.safe_dump(
                self._build_clab_document(lab_name=self.name or self.LAB_NAME),
                sort_keys=False,
            )
            from tempfile import NamedTemporaryFile

            with NamedTemporaryFile("w", suffix=".clab.yml", delete=False) as handle:
                handle.write(tmp)
                path = Path(handle.name)
            try:
                spec = parse_clab_topology(path)
            finally:
                path.unlink(missing_ok=True)
        spec.name = self.name or self.LAB_NAME
        return spec

    def get_info(self) -> str:
        inv = self.inventory
        return "\n".join(
            [
                f"Network Description: {self.desc}",
                f"SNDlib topology: {inv['topology_name']}",
                f"IGP: {inv['igp']}; metric_strategy: {inv['metric_strategy']}",
                f"BGP mode: {self.bgp_mode}",
                f"device_profile: {self.device_profile}",
                f"Edge stubs: {len(inv.get('hosts') or [])}",
                f"Inventory nodes: {inv['node_count']}; links: {inv['link_count']}",
            ]
        )

    def startup_verify_lab(self) -> dict:
        from nika.net_env.isp.containerlab.verify import verify_isp_srl_lab_startup

        return verify_isp_srl_lab_startup(
            self._build_runtime(),
            plan=self.plan,
            bgp_plan=self.bgp_plan,
            traffic=self.traffic,
            scenario_name=self.scenario_id,
        )

    def verify_lab(self) -> dict:
        from nika.net_env.isp.containerlab.verify import verify_isp_srl_lab

        return verify_isp_srl_lab(
            self._build_runtime(),
            plan=self.plan,
            bgp_plan=self.bgp_plan,
            traffic=self.traffic,
            scenario_name=self.scenario_id,
        )
