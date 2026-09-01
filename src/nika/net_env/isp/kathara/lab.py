"""Kathara ISP scenario compiled from SNDlib topologies."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from Kathara.manager.Kathara import Kathara
from Kathara.model.Lab import Lab

from nika.config import pkg_path
from nika.net_env.base import NetworkEnvBase
from nika.net_env.isp.bgp import (
    DEFAULT_BGP_MODE,
    BgpPlan,
    IspBgpMode,
    compile_bgp_plan,
    merge_frr_conf,
    normalize_bgp_mode,
    render_bgp_frr_fragment,
    scope_igp_to_bgp_as,
)
from nika.net_env.isp.igp import (
    DEFAULT_CONSTANT_METRIC,
    DEFAULT_IGP,
    DEFAULT_METRIC_STRATEGY,
    IspConfig,
    IspPlan,
    compile_isp_plan,
)
from nika.net_env.isp.traffic import (
    IspTrafficAttachment,
    TrafficInterval,
    TrafficMatrixSeries,
    attach_traffic_stubs,
)
from nika.net_env.isp.contract import (
    IspValidationPolicy,
    build_isp_validation_contract,
)
from nika.runtime.spec import NodeRole

IgpLiteral = Literal["isis", "ospf"]
MetricLiteral = Literal["constant", "routing_cost", "inv_capacity"]


def _stub_series_all_routers(plan: IspPlan) -> TrafficMatrixSeries:
    """Minimal series used only as attach_traffic_stubs layout metadata."""
    return TrafficMatrixSeries(
        topology=plan.topology_name,
        source="demands",
        intervals=(TrafficInterval(index=0, duration_sec=5, flows=()),),
        sample_period_sec=5,
        unit_note="stub-layout only",
        path=None,
    )


class Isp(NetworkEnvBase):
    LAB_NAME = "isp"
    TOPO_LEVEL = "medium"
    TOPO_SIZE = None
    TAGS = [
        "isp",
        "sndlib",
        "frr",
        "isis",
        "ospf",
        "bgp",
        "igp",
        "link",
        "icmp",
    ]

    def __init__(
        self,
        topo: str | Path | None = None,
        igp: IgpLiteral = DEFAULT_IGP,
        metric_strategy: MetricLiteral = DEFAULT_METRIC_STRATEGY,
        constant_metric: int = DEFAULT_CONSTANT_METRIC,
        bgp_mode: IspBgpMode | str = DEFAULT_BGP_MODE,
        rpki: bool = False,
        rtbh: bool = False,
        device_profile: str | None = None,
        topo_size: str | None = None,
        scenario_id: str | None = None,
        **kwargs,
    ):
        # Ignore legacy kwargs from old sessions.
        kwargs.pop("traffic_mode", None)
        kwargs.pop("traffic_scale", None)
        super().__init__(**kwargs)

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
        self.rtbh = bool(rtbh)
        self.bgp_mode: IspBgpMode = normalize_bgp_mode(
            raw_mode if isinstance(raw_mode, str) else raw_mode
        )
        if self.rpki and self.bgp_mode != "ebgp":
            raise ValueError(
                f"RPKI capability requires bgp_mode 'ebgp' (got {self.bgp_mode!r})."
            )
        if self.rtbh and self.bgp_mode != "ebgp":
            raise ValueError(
                f"RTBH capability requires bgp_mode 'ebgp' (got {self.bgp_mode!r})."
            )
        if self.rpki and self.rtbh:
            raise ValueError("RPKI and RTBH capabilities are mutually exclusive.")
        from nika.net_env.isp.profiles import (
            default_device_profile,
            normalize_device_profile,
            validate_backend_profile,
        )

        profile_raw = device_profile or default_device_profile("kathara")
        self.device_profile = normalize_device_profile(profile_raw)
        validate_backend_profile("kathara", self.device_profile)

        config = IspConfig(
            topology=self.topo,
            igp=igp,
            metric_strategy=metric_strategy,
            constant_metric=constant_metric,
        )
        base_plan = compile_isp_plan(config)
        self.bgp_plan: BgpPlan | None = compile_bgp_plan(
            base_plan, self.bgp_mode, rpki=self.rpki, rtbh=self.rtbh
        )
        base_plan = scope_igp_to_bgp_as(base_plan, self.bgp_plan)
        # Always attach edge stub hosts so traffic CLI can choose demands/dynamic later.
        stub_series = _stub_series_all_routers(base_plan)
        self.traffic: IspTrafficAttachment = attach_traffic_stubs(
            base_plan,
            stub_series,
            scale=1.0,
            pop_node_ids=tuple(n.node_id for n in base_plan.nodes),
        )
        self.plan: IspPlan = self.traffic.plan

        self.validation_policy = IspValidationPolicy()
        self.validation_contract = build_isp_validation_contract(
            self.plan,
            traffic=self.traffic,
            bgp_plan=self.bgp_plan,
            policy=self.validation_policy,
            scenario=self.scenario_id,
        )
        self.inventory = dict(self.plan.inventory)
        if self.bgp_plan is not None:
            self.inventory["bgp"] = self.bgp_plan.inventory

        node_n = len(self.plan.nodes)
        link_n = len(self.plan.links)
        host_n = len(self.traffic.hosts)
        per_node = 15 if self.plan.igp == "ospf" else 10
        cap = 2400 if self.plan.igp == "ospf" else 1200
        wait = max(180, min(cap, 60 + node_n * per_node + link_n + host_n * 5))
        if self.bgp_plan is not None:
            wait = max(wait, min(2400, 120 + node_n * 12 + link_n))
        if self.bgp_plan is not None and self.bgp_plan.inventory.get("rpki"):
            wait = max(wait, 900)
        self.VERIFY_MAX_WAIT_SEC = wait
        self.VERIFY_RETRY_DELAY_SEC = 5.0

        self.lab = Lab(self.scenario_id)
        self.name = self.scenario_id
        self.instance = Kathara.get_instance()
        bgp_desc = (
            f"BGP={self.bgp_mode}."
            if self.bgp_plan is not None
            else "Infrastructure loopbacks only; BGP disabled."
        )
        self.desc = (
            f"ISP from SNDlib topology '{self.plan.topology_name}' "
            f"({len(self.plan.nodes)} routers, {len(self.plan.links)} links, "
            f"{host_n} edge stubs, IGP={self.plan.igp}, "
            f"metric={self.plan.metric_strategy}). {bgp_desc}"
        )

        if self.bgp_plan is not None:
            rpki = bool(self.bgp_plan.inventory.get("rpki"))
            if self.plan.igp == "isis":
                daemons_pkg = (
                    "net_env/utils/kathara/isp/daemons_isis_bgp_rpki"
                    if rpki
                    else "net_env/utils/kathara/isp/daemons_isis_bgp"
                )
            else:
                daemons_pkg = (
                    "net_env/utils/kathara/isp/daemons_ospf_bgp_rpki"
                    if rpki
                    else "net_env/utils/kathara/isp/daemons_ospf_bgp"
                )
            vtysh_pkg = "net_env/utils/kathara/isp/vtysh.conf"
        else:
            daemons_pkg = (
                "net_env/utils/kathara/isis/daemons"
                if self.plan.igp == "isis"
                else "net_env/utils/kathara/ospf/daemons"
            )
            vtysh_pkg = (
                "net_env/utils/kathara/isis/vtysh.conf"
                if self.plan.igp == "isis"
                else "net_env/utils/kathara/ospf/vtysh.conf"
            )

        large = len(self.plan.nodes) >= 40 or self.bgp_plan is not None
        machine_opts = {
            "image": "nika/frr",
            "cpus": 1.0 if large else 0.5,
            "mem": "512m" if large else "256m",
        }
        machines = {}
        self.deployment_configs: dict[str, str] = {}
        for node in self.plan.nodes:
            machine = self.lab.new_machine(node.device_name, **machine_opts)
            capabilities = ["linux", "frr", self.plan.igp]
            if self.bgp_plan is not None:
                capabilities.append("bgp")
            if (
                self.bgp_plan is not None
                and self.bgp_plan.inventory.get("rpki")
                and node.device_name == self.bgp_plan.inventory.get("rov_observer")
            ):
                capabilities.append("rov")
            self.declare_machine(
                node.device_name,
                role=NodeRole.ROUTER,
                capabilities=tuple(capabilities),
            )
            machines[node.device_name] = machine

        for link in self.plan.links:
            self.lab.connect_machine_to_link(link.endpoint_a, link.collision_domain)
            self.lab.connect_machine_to_link(link.endpoint_b, link.collision_domain)

        host_opts = {"image": "nika/base", "cpus": 0.5, "mem": "256m"}
        for host in self.traffic.hosts:
            self.lab.new_machine(host.host_name, **host_opts)
            self.declare_machine(
                host.host_name,
                role=NodeRole.HOST,
                capabilities=("linux",),
                reachability_target=True,
            )
            self.lab.create_file_from_list(
                list(host.startup_commands),
                f"{host.host_name}.startup",
            )
        for edge in self.traffic.edge_links:
            self.lab.connect_machine_to_link(edge.router_device, edge.collision_domain)
            self.lab.connect_machine_to_link(edge.host_name, edge.collision_domain)

        self._rpki_attachment: dict | None = None
        if self.bgp_plan is not None and self.bgp_plan.inventory.get("rpki"):
            self._rpki_attachment = self._attach_routinator()

        bgp_by_device = {}
        if self.bgp_plan is not None:
            bgp_by_device = {n.device_name: n for n in self.bgp_plan.nodes}

        for node in self.plan.nodes:
            machine = machines[node.device_name]
            machine.create_file_from_path(
                str(pkg_path(daemons_pkg)), "/etc/frr/daemons"
            )
            machine.create_file_from_path(
                str(pkg_path(vtysh_pkg)), "/etc/frr/vtysh.conf"
            )
            frr_conf = node.frr_conf
            startup = list(node.startup_commands)
            bgp_node = bgp_by_device.get(node.device_name)
            if bgp_node is not None and self.bgp_plan is not None:
                fragment = render_bgp_frr_fragment(bgp_node, self.bgp_plan)
                frr_conf = merge_frr_conf(frr_conf, fragment)
                extras = []
                for pref in bgp_node.originated:
                    plen = pref.prefix.rsplit("/", 1)[1]
                    extras.append(f"ip addr add {pref.ping_address}/{plen} dev lo")
                if (
                    self._rpki_attachment is not None
                    and node.device_name == self._rpki_attachment["router"]
                ):
                    extras.append(
                        "ip addr add "
                        f"{self._rpki_attachment['router_address']}/"
                        f"{self._rpki_attachment['prefixlen']} "
                        f"dev {self._rpki_attachment['router_iface']}"
                    )
                if startup and startup[-1] == "service frr start":
                    post_frr: list[str] = []
                    if bgp_node is not None and bgp_node.rpki_cache is not None:
                        # RPKI module must be started after bgpd is up.
                        post_frr.append("vtysh -c 'rpki start'")
                    startup = startup[:-1] + extras + [startup[-1]] + post_frr
                else:
                    startup = startup + extras
                    if bgp_node is not None and bgp_node.rpki_cache is not None:
                        startup.append("vtysh -c 'rpki start'")
            machine.create_file_from_string(frr_conf, "/etc/frr/frr.conf")
            self.deployment_configs[node.device_name] = frr_conf
            self.lab.create_file_from_list(
                startup,
                f"{node.device_name}.startup",
            )

        self.load_machines()

    def _ensure_routinator_image(self) -> str:
        """Ensure the root-USER Routinator wrapper for Kathara startup."""
        from nika.net_env.utils.kathara.docker_files.docker_images import (
            ensure_nika_docker_images,
        )

        image = "nika/routinator:v0.14.2"
        ensure_nika_docker_images([image])
        return image

    def _attach_routinator(self) -> dict:
        """Attach an offline Routinator RTR next to the ROV observer."""
        import json

        from nika.net_env.isp.bgp.rpki_profile import (
            RPKI_COLLISION_DOMAIN,
            RPKI_PREFIXLEN,
            RPKI_ROUTER_ADDRESS,
            RPKI_ROUTINATOR_ADDRESS,
            RPKI_RTR_PORT,
            ROUTINATOR_MACHINE,
            slurm_document,
        )

        assert self.bgp_plan is not None
        rtr = self.bgp_plan.inventory.get("rpki_rtr") or {}
        router = str(rtr.get("router") or "")
        if not router:
            raise RuntimeError("RPKI inventory missing rpki_rtr.router")
        node = next(n for n in self.plan.nodes if n.device_name == router)
        router_iface = f"eth{len(node.interfaces)}"
        collision = str(rtr.get("collision_domain") or RPKI_COLLISION_DOMAIN)
        router_address = str(rtr.get("router_address") or RPKI_ROUTER_ADDRESS)
        routinator_address = str(rtr.get("address") or RPKI_ROUTINATOR_ADDRESS)
        prefixlen = int(rtr.get("prefixlen") or RPKI_PREFIXLEN)
        port = int(rtr.get("port") or RPKI_RTR_PORT)
        machine_name = str(rtr.get("machine") or ROUTINATOR_MACHINE)
        image = self._ensure_routinator_image()

        routinator = self.lab.new_machine(
            machine_name,
            **{
                "image": image,
                "shell": "/bin/sh",
                "entrypoint": "/bin/sh",
                "args": ["-c", "while true; do sleep 3600; done"],
                "cpus": 0.5,
                "mem": "256m",
            },
        )
        self.declare_machine(
            machine_name,
            role=NodeRole.INFRASTRUCTURE,
            capabilities=("linux", "rpki", "rtr"),
        )
        self.lab.connect_machine_to_link(router, collision)
        self.lab.connect_machine_to_link(machine_name, collision)

        slurm_body = json.dumps(slurm_document(), indent=2) + "\n"
        routinator.create_file_from_string(slurm_body, "/tmp/slurm.json")
        startup = [
            "mkdir -p /tmp/rpki-cache",
            f"ip addr add {routinator_address}/{prefixlen} dev eth0",
            "ip link set eth0 up",
            "routinator --repository-dir /tmp/rpki-cache "
            "--no-rir-tals --disable-rsync --disable-rrdp "
            "--exceptions /tmp/slurm.json "
            f"server --rtr 0.0.0.0:{port} --http 127.0.0.1:8323",
        ]
        self.lab.create_file_from_list(startup, f"{machine_name}.startup")
        return {
            "router": router,
            "router_iface": router_iface,
            "router_address": router_address,
            "prefixlen": prefixlen,
            "routinator": machine_name,
            "routinator_address": routinator_address,
            "port": port,
        }

    def get_info(self) -> str:
        base = super().get_info()
        inv = self.inventory
        lines = [
            base,
            f"SNDlib topology: {inv['topology_name']}",
            f"IGP: {inv['igp']}; metric_strategy: {inv['metric_strategy']}; "
            f"constant_metric: {inv['constant_metric']}",
            f"BGP mode: {self.bgp_mode}",
            f"Edge stubs: {len(inv.get('hosts') or [])} "
            f"(traffic matrix chosen at `nika traffic run sndlib`)",
            f"Inventory nodes: {inv['node_count']}; links: {inv['link_count']}",
        ]
        return "\n".join(lines)

    def startup_verify_lab(self) -> dict:
        from nika.net_env.isp.kathara.verify import verify_isp_lab_startup

        return verify_isp_lab_startup(
            self._build_runtime(),
            plan=self.plan,
            bgp_plan=self.bgp_plan,
            traffic=self.traffic,
            scenario_name=self.LAB_NAME,
        )

    def verify_lab(self) -> dict:
        from nika.net_env.isp.kathara.verify import verify_isp_lab

        return verify_isp_lab(
            self._build_runtime(),
            plan=self.plan,
            bgp_plan=self.bgp_plan,
            traffic=self.traffic,
            contract=self.validation_contract,
            scenario_name=self.LAB_NAME,
        )
