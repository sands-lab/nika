"""IOS-XR (XRd Control Plane) simple BGP lab.

Requires the XRd Control Plane image manually loaded and tagged as
``IMAGE`` — Cisco's licensing means it cannot be built automatically
like the ``nika/*`` images.
"""

import ipaddress

from Kathara.manager.Kathara import Kathara
from Kathara.model.Lab import Lab

from nika.net_env.base import NetworkEnvBase
from nika.net_env.utils.iosxr.common import (
    CONFIG_FILE_PATH,
    IMAGE,
    XR_ENTRYPOINT,
    XR_ZTP_DISABLE_ENV,
    build_xr_interfaces_env,
    build_xr_startup_script,
    require_xrd_image,
)
from nika.net_env.utils.pair_workload import DATA_SET_STARTUP
from nika.runtime.spec import NodeRole
from nika.net_env.utils.kathara.docker_files.docker_images import nika_image

LINK_IFACE = "GigabitEthernet0/0/0/0"
PC_IFACE = "GigabitEthernet0/0/0/1"

ROUTERS = {
    "router1": {
        "as": 1,
        "link_ip": ipaddress.ip_interface("193.10.11.1/24"),
        "peer_ip": ipaddress.ip_address("193.10.11.2"),
        "peer_as": 2,
        "pc_ip": ipaddress.ip_interface("195.11.14.1/24"),
        "network": ipaddress.ip_network("195.11.14.0/24"),
    },
    "router2": {
        "as": 2,
        "link_ip": ipaddress.ip_interface("193.10.11.2/24"),
        "peer_ip": ipaddress.ip_address("193.10.11.1"),
        "peer_as": 1,
        "pc_ip": ipaddress.ip_interface("200.1.1.1/24"),
        "network": ipaddress.ip_network("200.1.1.0/24"),
    },
}


def _build_startup_config(name: str, router: dict) -> str:
    return "\n".join(
        [
            f"hostname {name}",
            "!",
            f"interface {LINK_IFACE}",
            f" ipv4 address {router['link_ip']}",
            " no shutdown",
            "!",
            f"interface {PC_IFACE}",
            f" ipv4 address {router['pc_ip']}",
            " no shutdown",
            "!",
            "route-policy PASS",
            " pass",
            "end-policy",
            "!",
            f"router bgp {router['as']}",
            f" bgp router-id {router['link_ip'].ip}",
            " address-family ipv4 unicast",
            f"  network {router['network']}",
            " !",
            f" neighbor {router['peer_ip']}",
            f"  remote-as {router['peer_as']}",
            "  address-family ipv4 unicast",
            "   route-policy PASS in",
            "   route-policy PASS out",
            "  !",
            " !",
            "!",
            "end",
            "",
        ]
    )


class IosXrSimpleBGP(NetworkEnvBase):
    ROUTING_PARAMETER_KINDS = frozenset({"bgp_exports"})
    ROUTING_MODELS = frozenset({"iosxr-bgp"})
    BGP_ROUTERS = ROUTERS
    BGP_INTERFACE_MAP = {"eth0": LINK_IFACE, "eth1": PC_IFACE}
    BGP_PEERING_INTERFACE = LINK_IFACE
    BGP_CONFIG_PATH = CONFIG_FILE_PATH
    LAB_NAME = "iosxr_simple_bgp"
    VERIFY_MAX_WAIT_SEC = 480
    VERIFY_RETRY_DELAY_SEC = 10
    TOPO_LEVEL = "easy"

    def __init__(self, *, routing_parameters=None, **kwargs):
        super().__init__(**kwargs)
        self.lab = Lab(self.LAB_NAME)
        self.name = self.LAB_NAME
        self.instance = Kathara.get_instance()
        self.desc = (
            "A simple BGP network with two IOS-XR (XRd) routers and two pcs. "
            "The pcs replicate data sets to each other over HTTP, and pc1 keeps "
            "web sessions to pc2."
        )

        from nika.net_env.synthesis.vendor_bgp import render_iosxr_bgp_exports

        if routing_parameters is not None:
            from nika.net_env.synthesis.parameters import RoutingParameters

            solved = RoutingParameters.model_validate(routing_parameters)
            if any(policy.node not in ROUTERS for policy in solved.bgp_exports):
                raise ValueError("Unknown solved IOS-XR node")

        for router_name, router in ROUTERS.items():
            machine = self.lab.new_machine(router_name, **{"image": IMAGE})
            self.declare_machine(
                router_name,
                role=NodeRole.ROUTER,
                capabilities=("linux", "iosxr", "bgp"),
            )
            machine.add_meta("privileged", True)
            machine.add_meta("entrypoint", XR_ENTRYPOINT)
            machine.add_meta("ipv6", True)
            machine.add_meta(
                "env",
                build_xr_interfaces_env(
                    ("eth0", LINK_IFACE),
                    ("eth1", PC_IFACE),
                ),
            )
            machine.add_meta("env", XR_ZTP_DISABLE_ENV)
            config = _build_startup_config(router_name, router)
            if routing_parameters is not None:
                config = render_iosxr_bgp_exports(
                    config, router_name, routing_parameters
                )
            machine.create_file_from_string(config, CONFIG_FILE_PATH)
            self.lab.create_file_from_list(
                build_xr_startup_script(CONFIG_FILE_PATH, wait_iface=LINK_IFACE),
                f"{router_name}.startup",
            )

        pc1 = self.lab.new_machine("pc1", **{"image": nika_image("base")})
        pc2 = self.lab.new_machine("pc2", **{"image": nika_image("base")})
        self.declare_machine(
            pc1.name,
            role=NodeRole.HOST,
            capabilities=("linux",),
            reachability_target=True,
        )
        self.declare_machine(
            pc2.name,
            role=NodeRole.HOST,
            capabilities=("linux",),
            reachability_target=True,
        )

        self.lab.connect_machine_to_link("router1", "A")
        self.lab.connect_machine_to_link("router2", "A")

        self.lab.connect_machine_to_link("router1", "B")
        self.lab.connect_machine_to_link(pc1.name, "B")

        self.lab.connect_machine_to_link("router2", "C")
        self.lab.connect_machine_to_link(pc2.name, "C")

        self.lab.create_file_from_string(
            "ip addr add 195.11.14.2/24 dev eth0\n"
            "ip route add default via 195.11.14.1 dev eth0\n"
            f"{DATA_SET_STARTUP}\n",
            "pc1.startup",
        )
        self.lab.create_file_from_string(
            "ip addr add 200.1.1.2/24 dev eth0\n"
            "ip route add default via 200.1.1.1 dev eth0\n"
            f"{DATA_SET_STARTUP}\n",
            "pc2.startup",
        )

        self.load_machines()

    def business_workload(self):
        from nika.net_env.utils.pair_workload import build_pair_workload

        # pc1 -> router1 (eth1 in, eth0 out) -> router2 (eth0 in, eth1 out) -> pc2.
        return build_pair_workload(
            ("pc1", "195.11.14.2"),
            ("pc2", "200.1.1.2"),
            (("pc1", "eth0"), ("router1", "eth0"), ("router2", "eth1")),
            rate_limit_kbps=self.router_rate_limit_kbps(),
        )

    def bgp_origin_prefixes(self):
        return {str(router["network"]): name for name, router in ROUTERS.items()}

    def deploy(self):
        require_xrd_image(IMAGE)
        super().deploy()

    def startup_verify_lab(self) -> dict:
        from nika.net_env.iosxr_simple_bgp.verify import (
            verify_iosxr_simple_bgp_lab_startup,
        )

        return verify_iosxr_simple_bgp_lab_startup(
            self._build_runtime(), scenario_name=self.LAB_NAME
        )

    def verify_lab(self) -> dict:
        from nika.net_env.iosxr_simple_bgp.verify import (
            verify_iosxr_simple_bgp_lab,
        )

        return verify_iosxr_simple_bgp_lab(
            self._build_runtime(), scenario_name=self.LAB_NAME
        )
