"""Native ISP deployments sharing the topology, synthesis, and behavior model."""

from __future__ import annotations

from pathlib import Path

from Kathara.manager.Kathara import Kathara
from Kathara.model.Lab import Lab

from nika.net_env.base import NetworkEnvBase
from nika.net_env.isp.bgp import (
    compile_bgp_plan,
    normalize_bgp_mode,
    scope_igp_to_bgp_as,
)
from nika.net_env.isp.contract import build_isp_validation_contract
from nika.net_env.isp.igp import IspConfig, compile_isp_plan
from nika.net_env.isp.profiles import normalize_device_profile, validate_backend_profile
from nika.net_env.isp.traffic import (
    TrafficInterval,
    TrafficMatrixSeries,
    attach_traffic_stubs,
)
from nika.net_env.utils.kathara.docker_files.docker_images import (
    image_exists,
    nika_image,
)
from nika.runtime.spec import NodeRole


class IspVendor(NetworkEnvBase):
    """Compile any catalog ISP graph to a supported native OSPF/BGP platform."""

    ROUTING_PARAMETER_KINDS = frozenset({"ospf_costs", "bgp_imports"})
    LAB_NAME = "isp"
    TOPO_LEVEL = "medium"
    VERIFY_MAX_WAIT_SEC = 900
    VERIFY_RETRY_DELAY_SEC = 10

    def __init__(
        self,
        *,
        topo: str | Path,
        scenario_id: str,
        device_profile: str,
        igp: str = "ospf",
        metric_strategy: str = "constant",
        constant_metric: int = 10,
        bgp_mode: str = "none",
        rpki: bool = False,
        rtbh: bool = False,
        topo_size: str | None = None,
        routing_parameters: dict | None = None,
        routing_model: str | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.device_profile = normalize_device_profile(device_profile)
        validate_backend_profile(self.backend, self.device_profile)
        if self.device_profile not in {"iosxr", "routeros"}:
            raise ValueError("Native ISP requires iosxr or routeros")
        if igp != "ospf" or rpki or rtbh or topo_size is not None:
            raise ValueError(
                "Native ISP supports OSPF with optional BGP, without security overlays or topo_size"
            )
        model = f"{self.device_profile}-isp"
        if routing_model is not None and routing_model != model:
            raise ValueError(f"Expected routing_model={model!r}")
        self.ROUTING_MODELS = frozenset({model})
        self.topo, self.scenario_id = topo, scenario_id
        self.igp, self.metric_strategy, self.constant_metric = (
            igp,
            metric_strategy,
            constant_metric,
        )
        self.bgp_mode = normalize_bgp_mode(bgp_mode)
        self.rpki = self.rtbh = False
        config = IspConfig(
            topology=topo,
            igp=igp,
            metric_strategy=metric_strategy,
            constant_metric=constant_metric,
            first_interface_index=1 if self.device_profile == "routeros" else 0,
        )
        plan = compile_isp_plan(config)
        self.bgp_plan = compile_bgp_plan(plan, self.bgp_mode)
        plan = scope_igp_to_bgp_as(plan, self.bgp_plan)
        if routing_parameters is not None:
            from nika.net_env.synthesis.parameters import (
                bind_isp_imports,
                bind_isp_metrics,
            )

            plan = bind_isp_metrics(
                config,
                plan,
                self.bgp_plan,
                routing_parameters,
                parameter_kinds=self.ROUTING_PARAMETER_KINDS,
            )
            self.bgp_plan = bind_isp_imports(self.bgp_plan, routing_parameters)
        series = TrafficMatrixSeries(
            topology=plan.topology_name,
            source="demands",
            intervals=(TrafficInterval(index=0, duration_sec=5, flows=()),),
            sample_period_sec=5,
            unit_note="stub-layout only",
            path=None,
        )
        self.traffic = attach_traffic_stubs(
            plan,
            series,
            scale=1.0,
            pop_node_ids=tuple(node.node_id for node in plan.nodes),
            render_frr=False,
        )
        self.plan = self.traffic.plan
        self.inventory = dict(self.plan.inventory)
        if self.bgp_plan is not None:
            self.inventory["bgp"] = self.bgp_plan.inventory
        self.validation_contract = build_isp_validation_contract(
            self.plan,
            traffic=self.traffic,
            bgp_plan=self.bgp_plan,
            scenario=scenario_id,
        )
        if self.device_profile == "iosxr":
            from nika.net_env.isp.iosxr import iosxr_interface, render_iosxr_isp
            from nika.net_env.utils.iosxr.common import CONFIG_FILE_PATH, IMAGE

            native_interface = iosxr_interface
            self.deployment_configs = render_iosxr_isp(self.plan, self.bgp_plan)
        else:
            from nika.net_env.isp.routeros import (
                render_routeros_isp,
                routeros_interface,
            )
            from nika.net_env.routeros_simple_bgp.lab import CONFIG_FILE_PATH, IMAGE

            native_interface = routeros_interface
            self.deployment_configs = render_routeros_isp(self.plan, self.bgp_plan)
        self.BGP_CONFIG_PATH = CONFIG_FILE_PATH
        self.BGP_ROUTERS = {node.device_name: node for node in self.plan.nodes}
        self.BGP_INTERFACE_MAP = {
            iface.name: native_interface(iface.name)
            for node in self.plan.nodes
            for iface in node.interfaces
        }
        self.BGP_INTERFACE_MAP["lo"] = native_interface("lo")
        self._image = IMAGE
        self.lab = Lab(scenario_id)
        self.name = scenario_id
        self.instance = Kathara.get_instance()
        self.desc = f"ISP {self.plan.topology_name}: {self.device_profile}, OSPF, BGP={self.bgp_mode}"
        for node in self.plan.nodes:
            machine = self.lab.new_machine(
                node.device_name,
                image=IMAGE,
                cpus=1.0,
                mem="3g" if self.device_profile == "iosxr" else "1g",
            )
            machine.add_meta("privileged", True)
            machine.create_file_from_string(
                self.deployment_configs[node.device_name], CONFIG_FILE_PATH
            )
            capabilities = ("linux", self.device_profile, "ospf")
            if self.bgp_plan is not None:
                capabilities += ("bgp",)
            self.declare_machine(
                node.device_name, role=NodeRole.ROUTER, capabilities=capabilities
            )
            if self.device_profile == "iosxr":
                from nika.net_env.utils.iosxr.common import (
                    XR_ENTRYPOINT,
                    XR_ZTP_DISABLE_ENV,
                    build_xr_interfaces_env,
                    build_xr_startup_script,
                )

                machine.add_meta("ipv6", True)
                machine.add_meta("entrypoint", XR_ENTRYPOINT)
                machine.add_meta(
                    "env",
                    build_xr_interfaces_env(
                        *(
                            (iface.name, native_interface(iface.name))
                            for iface in node.interfaces
                        )
                    ),
                )
                machine.add_meta("env", XR_ZTP_DISABLE_ENV)
                self.lab.create_file_from_list(
                    build_xr_startup_script(
                        CONFIG_FILE_PATH,
                        wait_iface=native_interface(node.interfaces[0].name),
                    ),
                    f"{node.device_name}.startup",
                )
            else:
                machine.add_meta("env", f"CLAB_INTFS={len(node.interfaces)}")
                machine.add_meta("args", "--connection-mode macvtap")
                self.lab.connect_machine_to_link(
                    node.device_name,
                    f"{node.device_name}_reserved0",
                    machine_iface_number=0,
                )
        for link in self.plan.links:
            self.lab.connect_machine_to_link(
                link.endpoint_a,
                link.collision_domain,
                machine_iface_number=int(link.iface_a[3:]),
            )
            self.lab.connect_machine_to_link(
                link.endpoint_b,
                link.collision_domain,
                machine_iface_number=int(link.iface_b[3:]),
            )
        for host in self.traffic.hosts:
            self.lab.new_machine(
                host.host_name,
                image=nika_image("base"),
                shell="/bin/sh",
                mem="256m",
                cpus=0.5,
            )
            self.declare_machine(
                host.host_name,
                role=NodeRole.HOST,
                capabilities=("linux",),
                reachability_target=True,
            )
            self.lab.create_file_from_list(
                list(host.startup_commands), f"{host.host_name}.startup"
            )
        for edge in self.traffic.edge_links:
            self.lab.connect_machine_to_link(
                edge.router_device,
                edge.collision_domain,
                machine_iface_number=int(edge.router_iface[3:]),
            )
            self.lab.connect_machine_to_link(
                edge.host_name, edge.collision_domain, machine_iface_number=0
            )
        self.load_machines()

    def deploy(self):
        if not image_exists(self._image):
            raise RuntimeError(
                f"Native ISP image {self._image!r} must be installed locally"
            )
        # Conservative admission estimate, not a bound on vendor process usage.
        # Reject oversized native deployments before creating any containers.
        available = next(
            int(line.split()[1]) * 1024
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith("MemAvailable:")
        )
        gib = 1024**3
        per_router = 3 * gib if self.device_profile == "iosxr" else gib
        required = (
            2 * gib
            + len(self.plan.nodes) * per_router
            + len(self.traffic.hosts) * (gib // 4)
        )
        if required > available:
            raise RuntimeError(
                f"Native ISP {self.device_profile} with {len(self.plan.nodes)} routers "
                f"needs an estimated {required / gib:.1f} GiB including headroom; "
                f"only {available / gib:.1f} GiB is available. Use a smaller topology "
                "or a host with more memory."
            )
        super().deploy()

    def get_info(self):
        from nika.net_env.isp.brief import format_isp_network_info

        return format_isp_network_info(
            super().get_info(),
            inventory=self.inventory,
            bgp_mode=self.bgp_mode,
            device_profile=self.device_profile,
        )

    def startup_verify_lab(self):
        # Readiness checks adjacencies and edge-host ICMP; the experiment checks
        # the complete original behavior and exact forwarding requirements.
        from nika.net_env.vendor_validation import (
            verify_iosxr_contract,
            verify_routeros_contract,
        )
        from nika.net_env.verify import build_lab_verify_result

        contract = self.validation_contract.model_copy(
            update={
                "intents": tuple(
                    i
                    for i in self.validation_contract.intents
                    if i.adjacency is not None
                    or (
                        i.property == "reachability"
                        and i.traffic.protocol == "icmp"
                        and i.source.kind == "endpoint"
                        and i.destination.kind == "endpoint"
                    )
                )
            }
        )
        verifier = (
            verify_iosxr_contract
            if self.device_profile == "iosxr"
            else verify_routeros_contract
        )
        report = verifier(self._build_runtime(), contract=contract, plan=self.plan)
        checks = {row.intent: row.status == "passed" for row in report.results}
        return build_lab_verify_result(
            scenario_name=self.scenario_id,
            verified=bool(checks) and all(checks.values()),
            checks=checks,
            details=report.model_dump(mode="json"),
        )

    def verify_lab(self):
        return self.startup_verify_lab()
