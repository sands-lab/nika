"""IOS-XR (XRd Control Plane) simple BGP lab.

Requires the XRd Control Plane image manually loaded and tagged as
``IMAGE`` — Cisco's licensing means it cannot be built automatically
like the ``nika/*`` images.
"""

import ipaddress

from Kathara.manager.Kathara import Kathara
from Kathara.model.Lab import Lab

from nika.net_env.base import NetworkEnvBase
from nika.net_env.iosxr.common import (
    CONFIG_FILE_PATH,
    IMAGE,
    XR_ZTP_DISABLE_ENV,
    build_xr_interfaces_env,
    build_xr_startup_script,
    require_xrd_image,
)
from nika.runtime.spec import NodeRole

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
    LAB_NAME = "iosxr_simple_bgp"
    VERIFY_MAX_WAIT_SEC = 480
    VERIFY_RETRY_DELAY_SEC = 10
    TOPO_LEVEL = "easy"
    TOPO_SIZE = None
    TAGS = ["arp", "link", "bgp", "icmp", "iosxr", "pc"]

    def __init__(self, **kwargs):
        super().__init__()
        self.lab = Lab(self.LAB_NAME)
        self.name = self.LAB_NAME
        self.instance = Kathara.get_instance()
        self.desc = "A simple BGP network with two IOS-XR (XRd) routers and two pcs."

        for router_name, router in ROUTERS.items():
            machine = self.lab.new_machine(router_name, **{"image": IMAGE})
            self.declare_machine(
                router_name,
                role=NodeRole.ROUTER,
                capabilities=("linux", "iosxr", "bgp"),
            )
            machine.add_meta("privileged", True)
            machine.add_meta("ipv6", True)
            machine.add_meta(
                "env",
                build_xr_interfaces_env(
                    ("eth0", LINK_IFACE),
                    ("eth1", PC_IFACE),
                ),
            )
            machine.add_meta("env", XR_ZTP_DISABLE_ENV)
            machine.create_file_from_string(
                _build_startup_config(router_name, router), CONFIG_FILE_PATH
            )
            self.lab.create_file_from_list(
                build_xr_startup_script(CONFIG_FILE_PATH, wait_iface=LINK_IFACE),
                f"{router_name}.startup",
            )

        pc1 = self.lab.new_machine("pc1", **{"image": "nika/base"})
        pc2 = self.lab.new_machine("pc2", **{"image": "nika/base"})
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

    def deploy(self):
        require_xrd_image(IMAGE)
        super().deploy()

    def startup_verify_lab(self) -> dict:
        from nika.net_env.kathara.interdomain_routing.iosxr_simple_bgp.verify import (
            verify_iosxr_simple_bgp_lab_startup,
        )

        return verify_iosxr_simple_bgp_lab_startup(
            self._build_runtime(), scenario_name=self.LAB_NAME
        )

    def verify_lab(self) -> dict:
        from nika.net_env.kathara.interdomain_routing.iosxr_simple_bgp.verify import (
            verify_iosxr_simple_bgp_lab,
        )

        return verify_iosxr_simple_bgp_lab(
            self._build_runtime(), scenario_name=self.LAB_NAME
        )
