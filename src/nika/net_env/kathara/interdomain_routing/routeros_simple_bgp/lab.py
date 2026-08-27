"""MikroTik RouterOS (vrnetlab) equivalent of ``simple_bgp``.

Requires a locally built ``vrnetlab/mikrotik/routeros`` image tagged as
``IMAGE`` below — MikroTik's licensing means the CHR disk image cannot be
redistributed or auto-built like the ``kathara/nika-*`` images.

RouterOS via vrnetlab boots a full QEMU VM inside the container (unlike XRd,
which is a native container process). vrnetlab always reserves the first
container interface (``eth0`` / RouterOS ``ether1``) for its own internal
management bridge, so each router here starts with an isolated placeholder
link that consumes Kathara interface index 0 before the real data links are
attached — see the two ``connect_machine_to_link`` calls below.
"""

import ipaddress

from Kathara.manager.Kathara import Kathara
from Kathara.model.Lab import Lab

from nika.net_env.base import NetworkEnvBase
from nika.net_env.kathara.utils.docker_files.docker_images import image_exists

IMAGE = "vrnetlab/mikrotik_routeros:7.21.5"

# vrnetlab's own fixed, non-secret internal defaults for RouterOS's mgmt IP,
# which sits on an internal bridge reachable only via `docker exec` into the
# container's network namespace — never exposed on the Kathara data plane.
MGMT_USER = "vrnetlab"
MGMT_PASSWORD = "VR-netlab9"
MGMT_ADDR = "172.31.255.30"

# Real data-link count per router (router-to-router + router-to-pc). Must
# track this exactly: vrnetlab's boot blocks with no timeout until it sees
# CLAB_INTFS + 1 total container interfaces, so setting this too high hangs
# the VM forever waiting for interfaces this scenario never attaches.
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
    LAB_NAME = "routeros_simple_bgp"
    # Nested QEMU boot is slower than XRd's native-container boot.
    VERIFY_MAX_WAIT_SEC = 600
    VERIFY_RETRY_DELAY_SEC = 10
    TOPO_LEVEL = "easy"
    TOPO_SIZE = None
    TAGS = ["arp", "link", "bgp", "icmp", "routeros", "pc"]

    def __init__(self, **kwargs):
        super().__init__()
        self.lab = Lab(self.LAB_NAME)
        self.name = self.LAB_NAME
        self.instance = Kathara.get_instance()
        self.desc = (
            "A simple BGP network with two MikroTik RouterOS routers and two pcs."
        )

        for router_name, router in ROUTERS.items():
            machine = self.lab.new_machine(router_name, **{"image": IMAGE})
            machine.add_meta("privileged", True)
            machine.add_meta("env", f"CLAB_INTFS={CLAB_INTFS}")
            # vrnetlab's default "vrxcon" datapath only wires a qemu netdev
            # for a data interface that appears *after* boot (containerlab's
            # model). Kathara attaches interfaces before the container
            # starts, so eth1/eth2 already exist and vrxcon's -device ends up
            # with no matching -netdev, crashing qemu immediately. "macvtap"
            # instead wraps the already-present interface in a passthru
            # macvtap, which is the datapath that matches Kathara's model.
            machine.add_meta("args", "--connection-mode macvtap")
            machine.create_file_from_string(
                _build_startup_config(router_name, router),
                "/ftpboot/config.auto.rsc",
            )
            # Consumes Kathara interface index 0 (RouterOS `ether1`, reserved
            # by vrnetlab for its own mgmt bridge). Must be connected first.
            self.lab.connect_machine_to_link(router_name, f"{router_name}_reserved0")

        pc1 = self.lab.new_machine("pc1", **{"image": "kathara/nika-base"})
        pc2 = self.lab.new_machine("pc2", **{"image": "kathara/nika-base"})

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
        if not image_exists(IMAGE):
            raise RuntimeError(
                f"MikroTik RouterOS image {IMAGE!r} not found locally. It "
                "must be built by hand from a downloaded CHR image, e.g.:\n"
                "  git clone https://github.com/hellt/vrnetlab && "
                "cd vrnetlab/mikrotik/routeros\n"
                "  # copy the downloaded CHR .vmdk/.vdi into this directory\n"
                "  make docker-image\n"
                f"  docker tag <built-tag> {IMAGE}\n"
                "See docs/mikrotik-routeros-setup.md for the full procedure."
            )
        super().deploy()

    def verify_lab(self) -> dict:
        from nika.net_env.kathara.interdomain_routing.routeros_simple_bgp.verify import (
            verify_routeros_simple_bgp_lab,
        )

        return verify_routeros_simple_bgp_lab(
            self._build_runtime(), scenario_name=self.LAB_NAME
        )
