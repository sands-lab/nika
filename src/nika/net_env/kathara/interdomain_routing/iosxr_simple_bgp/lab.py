"""IOS-XR (XRd Control Plane) simple BGP lab.

Requires the XRd Control Plane image manually loaded and tagged as
``IMAGE`` below — Cisco's licensing means it cannot be built automatically
like the ``nika/*`` images.
"""

import ipaddress

from Kathara.manager.Kathara import Kathara
from Kathara.model.Lab import Lab

from nika.net_env.base import NetworkEnvBase
from nika.net_env.utils.kathara.docker_files.docker_images import image_exists
from nika.runtime.spec import NodeRole

IMAGE = "ios-xr/xrd-control-plane:26.2.1"

LINK_IFACE = "GigabitEthernet0/0/0/0"
PC_IFACE = "GigabitEthernet0/0/0/1"
CONFIG_FILE_PATH = "disk0:/startup-config.cfg"

CLI_COMMAND = '/pkg/bin/xr_cli "{command}"'
ZTP_APPLY_COMMAND = "/bin/bash -c 'source /pkg/bin/ztp_helper.sh; xrapply {file}'"

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


def _build_startup_script(config_path: str) -> list:
    check_cmd_1 = (
        CLI_COMMAND.format(command="show run")
        + " | egrep -e 'No configuration change' -e 'No such file or directory'"
    )
    check_cmd_2 = (
        CLI_COMMAND.format(command=f"sh ip interface {LINK_IFACE}")
        + " | egrep -e 'ipv4 protocol is Down'"
    )
    check_cmd_3 = (
        CLI_COMMAND.format(command=f"sh ipv6 interface {LINK_IFACE}")
        + " | egrep -e 'ipv6 protocol is Down'"
    )

    # xrapply can fail with "Cannot open network namespace" if it runs the
    # instant Kathara's startup script starts, before XR has finished
    # creating the "xrnns" netns: the config is silently never applied and
    # the container stays "running" with the data interfaces down. Retry
    # until that specific error goes away.
    apply_cmd = ZTP_APPLY_COMMAND.format(file=config_path)
    apply_with_retry = "\n".join(
        [
            "apply_ok=0",
            "for _xr_apply_try in $(seq 1 40); do",
            f"  _xr_apply_out=$({apply_cmd} 2>&1)",
            '  echo "$_xr_apply_out"',
            '  if ! echo "$_xr_apply_out" | grep -q "Cannot open network namespace"; then',
            "    apply_ok=1",
            "    break",
            "  fi",
            "  sleep 3",
            "done",
            '[[ "$apply_ok" -eq 1 ]] || echo "ERROR: xrapply did not succeed after retries"',
        ]
    )

    return [
        "pgrep xrd-startup; while [[ $? -eq 0 ]]; do sleep 3; pgrep xrd-startup; done",
        check_cmd_1,
        f"while [[ $? -eq 0 ]]; do sleep 3; {check_cmd_1}; done",
        check_cmd_2,
        f"while [[ $? -eq 0 ]]; do sleep 3; {check_cmd_2}; done",
        check_cmd_3,
        f"while [[ $? -eq 0 ]]; do sleep 3; {check_cmd_3}; done",
        "source /pkg/bin/ztp_helper.sh; ztp_disable; ztp_kill_all; killall -9 pyztp2",
        apply_with_retry,
    ]


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
                f"XR_INTERFACES=linux:eth0,xr_name={LINK_IFACE};linux:eth1,xr_name={PC_IFACE}",
            )
            machine.add_meta("env", "XR_ZTP_ENABLE=0")
            machine.create_file_from_string(
                _build_startup_config(router_name, router), CONFIG_FILE_PATH
            )
            self.lab.create_file_from_list(
                _build_startup_script(CONFIG_FILE_PATH), f"{router_name}.startup"
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
        if not image_exists(IMAGE):
            raise RuntimeError(
                f"XRd Control Plane image {IMAGE!r} not found locally. Cisco's "
                "license requires loading it by hand, e.g.:\n"
                "  docker load -i xrd-control-plane-container-x86.<version>.tgz\n"
                f"  docker tag <loaded-tag> {IMAGE}"
            )
        super().deploy()

    def verify_lab(self) -> dict:
        from nika.net_env.kathara.interdomain_routing.iosxr_simple_bgp.verify import (
            verify_iosxr_simple_bgp_lab,
        )

        return verify_iosxr_simple_bgp_lab(
            self._build_runtime(), scenario_name=self.LAB_NAME
        )
