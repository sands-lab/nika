"""MikroTik RouterOS (vrnetlab) simple BGP lab.

Requires a locally built ``vrnetlab/mikrotik/routeros`` image tagged as
``IMAGE`` — MikroTik's licensing means the CHR disk image cannot be
redistributed or auto-built like the ``nika/*`` images. Machine setup, including
the reserved management interface, is shared in ``nika.net_env.utils.routeros``.
"""

import ipaddress

from Kathara.manager.Kathara import Kathara
from Kathara.model.Lab import Lab

from nika.net_env.base import NetworkEnvBase
from nika.net_env.utils.kathara.docker_files.docker_images import nika_image
from nika.net_env.utils.routeros.common import (
    CONFIG_FILE_PATH,
    IMAGE,
    add_routeros_router,
    require_routeros_image,
)
from nika.runtime.spec import NodeRole
from nika.service.lab.routeros_api import MGMT_ADDR, MGMT_PASSWORD, MGMT_USER

__all__ = [
    "CONFIG_FILE_PATH",
    "IMAGE",
    "MGMT_ADDR",
    "MGMT_PASSWORD",
    "MGMT_USER",
    "RouterOsSimpleBGP",
]

# Real data-link count per router (router-to-router + router-to-pc).
CLAB_INTFS = 2

LINK_IFACE = "ether2"
PC_IFACE = "ether3"

ROUTERS = {
    "router1": {
        "as": 1,
        "link_ip": ipaddress.ip_interface("193.10.11.1/24"),
        "peer_ip": ipaddress.ip_address("193.10.11.2"),
        "peer_as": 2,
        "pc_ip": ipaddress.ip_interface("195.11.14.1/24"),
    },
    "router2": {
        "as": 2,
        "link_ip": ipaddress.ip_interface("193.10.11.2/24"),
        "peer_ip": ipaddress.ip_address("193.10.11.1"),
        "peer_as": 1,
        "pc_ip": ipaddress.ip_interface("200.1.1.1/24"),
    },
}


def _build_startup_config(name: str, router: dict) -> str:
    # RouterOS auto-imports a file named exactly `config.auto.rsc` as soon as
    # vrnetlab FTPs it in (MikroTik's *.auto.rsc convention); the importer
    # wants terse, single-line commands, so this is authored directly in
    # that form.
    #
    # This targets RouterOS 7's BGP menu, which replaced ROS6's: there is no
    # implicit "default" instance, so one must be created explicitly with
    # its own `as=`/`router-id=`; `/routing bgp connection add` takes no
    # `router-id=` of its own (it comes from the instance); and
    # `/routing bgp network add` no longer exists — the PC-facing subnet is
    # advertised by redistributing connected routes instead.
    return "\n".join(
        [
            f"/system identity set name={name}",
            f"/ip address add address={router['link_ip']} interface={LINK_IFACE}",
            f"/ip address add address={router['pc_ip']} interface={PC_IFACE}",
            (
                "/routing bgp instance add name=default "
                f"as={router['as']} router-id={router['link_ip'].ip}"
            ),
            (
                "/routing bgp connection add name=to-peer instance=default "
                f"local.role=ebgp local.address={router['link_ip'].ip} "
                f"remote.address={router['peer_ip']} remote.as={router['peer_as']} "
                "output.redistribute=connected"
            ),
            "",
        ]
    )


class RouterOsSimpleBGP(NetworkEnvBase):
    ROUTING_PARAMETER_KINDS = frozenset({"bgp_exports"})
    ROUTING_MODELS = frozenset({"routeros-bgp"})
    BGP_ROUTERS = ROUTERS
    BGP_INTERFACE_MAP = {"eth1": LINK_IFACE, "eth2": PC_IFACE}
    BGP_PEERING_INTERFACE = LINK_IFACE
    BGP_CONFIG_PATH = CONFIG_FILE_PATH
    LAB_NAME = "routeros_simple_bgp"
    # Nested QEMU boot is slower than XRd's native-container boot.
    VERIFY_MAX_WAIT_SEC = 600
    VERIFY_RETRY_DELAY_SEC = 10
    TOPO_LEVEL = "easy"

    def __init__(self, *, routing_parameters=None, **kwargs):
        super().__init__(**kwargs)
        self.lab = Lab(self.LAB_NAME)
        self.name = self.LAB_NAME
        self.instance = Kathara.get_instance()
        self.desc = (
            "A simple BGP network with two MikroTik RouterOS routers and two pcs."
        )

        from nika.net_env.synthesis.vendor_bgp import render_routeros_bgp_exports

        if routing_parameters is not None:
            from nika.net_env.synthesis.parameters import RoutingParameters

            solved = RoutingParameters.model_validate(routing_parameters)
            if any(policy.node not in ROUTERS for policy in solved.bgp_exports):
                raise ValueError("Unknown solved RouterOS node")

        for router_name, router in ROUTERS.items():
            config = _build_startup_config(router_name, router)
            if routing_parameters is not None:
                config = render_routeros_bgp_exports(
                    config, router_name, routing_parameters
                )
            add_routeros_router(
                self.lab, router_name, config=config, data_interfaces=CLAB_INTFS
            )
            self.declare_machine(
                router_name,
                role=NodeRole.ROUTER,
                capabilities=("linux", "routeros", "bgp"),
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
            "ip route add default via 195.11.14.1 dev eth0\n",
            "pc1.startup",
        )
        self.lab.create_file_from_string(
            "ip addr add 200.1.1.2/24 dev eth0\n"
            "ip route add default via 200.1.1.1 dev eth0\n",
            "pc2.startup",
        )

        self.load_machines()

    def bgp_origin_prefixes(self):
        return {str(router["pc_ip"].network): name for name, router in ROUTERS.items()}

    def deploy(self):
        require_routeros_image()
        super().deploy()

    def startup_verify_lab(self) -> dict:
        from nika.net_env.routeros_simple_bgp.verify import (
            verify_routeros_simple_bgp_lab_startup,
        )

        return verify_routeros_simple_bgp_lab_startup(
            self._build_runtime(), scenario_name=self.LAB_NAME
        )

    def verify_lab(self) -> dict:
        from nika.net_env.routeros_simple_bgp.verify import (
            verify_routeros_simple_bgp_lab,
        )

        return verify_routeros_simple_bgp_lab(
            self._build_runtime(), scenario_name=self.LAB_NAME
        )
