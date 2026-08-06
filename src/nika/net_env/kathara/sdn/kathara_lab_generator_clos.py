#!/usr/bin/env python3
"""
Standalone lab generator for SDN Clos (spine-leaf) topology.
Generates Kathara-compatible lab configuration WITHOUT Kathara dependency.
Output: topology/ folder containing lab.conf and *.startup.
"""

import os
from dataclasses import dataclass, field
from ipaddress import IPv4Network
from typing import Literal

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

from nika.net_env.kathara.sdn.ovs_startup import ovs_start_commands  # noqa: E402


@dataclass
class SwitchMeta:
    name: str
    eth_index: int = 0
    cmd_list: list[str] = field(default_factory=list)
    image: str = "kathara/sdn"
    cpus: float = 0.5
    mem: str = "512m"
    links: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class HostMeta:
    name: str
    eth_index: int = 0
    cmd_list: list[str] = field(default_factory=list)
    image: str = "kathara/nika-base"
    cpus: float = 0.5
    mem: str = "256m"
    links: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class ControllerMeta:
    name: str
    cmd_list: list[str] = field(default_factory=list)
    image: str = "kathara/nika-pox"
    cpus: float = 0.5
    mem: str = "256m"
    links: list[tuple[int, str]] = field(default_factory=list)
    bridged: bool = True


def generate_sdn_clos_topology(
    topo_size: Literal["s", "m", "l"] = "s",
    output_dir: str | None = None,
) -> str:
    """
    Generate Kathara-compatible lab configuration for SDN Clos topology.
    Returns the absolute path to the output directory.
    """
    if output_dir is None:
        output_dir = os.path.join(SCRIPT_DIR, "topology")

    if topo_size == "s":
        SPINE_NUM, LEAF_NUM, HOST_PER_LEAF = 1, 2, 2
    elif topo_size == "m":
        SPINE_NUM, LEAF_NUM, HOST_PER_LEAF = 2, 4, 4
    elif topo_size == "l":
        SPINE_NUM, LEAF_NUM, HOST_PER_LEAF = 4, 8, 8
    else:
        raise ValueError("topo_size should be 's', 'm', or 'l'.")

    spine_switches: list[SwitchMeta] = []
    for i in range(SPINE_NUM):
        spine_switches.append(SwitchMeta(name=f"spine_{i + 1}"))

    leaf_switches: list[SwitchMeta] = []
    for i in range(LEAF_NUM):
        leaf_switches.append(SwitchMeta(name=f"leaf_{i + 1}"))

    tot_switch_list = spine_switches + leaf_switches

    tot_host_list: list[HostMeta] = []
    for leaf_id in range(LEAF_NUM):
        for host_id in range(HOST_PER_LEAF):
            tot_host_list.append(HostMeta(name=f"pc_{leaf_id + 1}_{host_id + 1}"))

    controller = ControllerMeta(name="controller")

    for switch_meta in tot_switch_list:
        switch_meta.cmd_list.extend(ovs_start_commands())
        switch_meta.cmd_list.append(f"ovs-vsctl add-br {switch_meta.name}")
        switch_meta.cmd_list.append(
            f"ovs-vsctl set-fail-mode {switch_meta.name} secure"
        )

    host_network = IPv4Network("10.0.0.0/24")
    host_pool = list(host_network.hosts())
    idx = 0
    for leaf_idx, leaf_switch in enumerate(leaf_switches, start=1):
        leaf_hosts = [h for h in tot_host_list if h.name.startswith(f"pc_{leaf_idx}_")]
        for host_meta in leaf_hosts:
            link_name = f"{host_meta.name}_{leaf_switch.name}"
            host_meta.links.append((0, link_name))
            leaf_switch.links.append((leaf_switch.eth_index, link_name))
            host_ip = str(host_pool[idx])
            idx += 1
            host_meta.cmd_list.append(f"ip addr add {host_ip}/24 dev eth0")
            host_meta.cmd_list.append("ip link set eth0 up")
            leaf_switch.cmd_list.append(
                f"ovs-vsctl add-port {leaf_switch.name} eth{leaf_switch.eth_index}"
            )
            leaf_switch.cmd_list.append(f"ip link set eth{leaf_switch.eth_index} up")
            leaf_switch.eth_index += 1

    for spine in spine_switches:
        for leaf in leaf_switches:
            link_name = f"{spine.name}_{leaf.name}"
            spine.links.append((spine.eth_index, link_name))
            leaf.links.append((leaf.eth_index, link_name))
            spine.cmd_list.append(
                f"ovs-vsctl add-port {spine.name} eth{spine.eth_index}"
            )
            spine.eth_index += 1
            leaf.cmd_list.append(f"ovs-vsctl add-port {leaf.name} eth{leaf.eth_index}")
            leaf.eth_index += 1

    controller_ip = "20.0.0.100"
    infra_network = list(IPv4Network("20.0.0.0/24").hosts())
    for switch_meta in tot_switch_list:
        switch_meta.links.append((switch_meta.eth_index, "switch_controller"))
        switch_ip = str(infra_network.pop(0))
        switch_meta.cmd_list.append(
            f"ip addr add {switch_ip}/24 dev eth{switch_meta.eth_index}"
        )
        switch_meta.cmd_list.append(f"ip link set eth{switch_meta.eth_index} up")
        switch_meta.cmd_list.append(
            f"ovs-vsctl set-controller {switch_meta.name} tcp:{controller_ip}:6633"
        )
        switch_meta.eth_index += 1
    controller.links.append((0, "switch_controller"))
    controller.cmd_list = [
        "ip addr add 20.0.0.100/24 dev eth0",
        "ip link set eth0 up",
        "python3 /pox/pox.py forwarding.l2_learning &",
    ]

    out_path = os.path.abspath(output_dir)
    os.makedirs(out_path, exist_ok=True)

    lab_conf_lines = [
        'LAB_NAME="sdn_clos"',
        'LAB_DESCRIPTION="SDN Clos topology - generated by kathara_lab_generator_clos"',
        "",
    ]
    for meta in tot_switch_list + tot_host_list:
        for eth_idx, cd in meta.links:
            lab_conf_lines.append(f'{meta.name}[{eth_idx}]="{cd}"')
        lab_conf_lines.append(f'{meta.name}[image]="{meta.image}"')
        lab_conf_lines.append(f"{meta.name}[cpus]={meta.cpus}")
        lab_conf_lines.append(f'{meta.name}[mem]="{meta.mem}"')
        lab_conf_lines.append("")
    for eth_idx, cd in controller.links:
        lab_conf_lines.append(f'{controller.name}[{eth_idx}]="{cd}"')
    lab_conf_lines.append(f'{controller.name}[image]="{controller.image}"')
    lab_conf_lines.append(f"{controller.name}[cpus]={controller.cpus}")
    lab_conf_lines.append(f'{controller.name}[mem]="{controller.mem}"')
    lab_conf_lines.append(
        f"{controller.name}[bridged]={'true' if controller.bridged else 'false'}"
    )
    lab_conf_lines.append("")

    with open(os.path.join(out_path, "lab.conf"), "w", encoding="utf-8") as f:
        f.write("\n".join(lab_conf_lines))

    for meta in tot_switch_list + tot_host_list:
        with open(
            os.path.join(out_path, f"{meta.name}.startup"), "w", encoding="utf-8"
        ) as f:
            f.write("\n".join(meta.cmd_list))
    with open(
        os.path.join(out_path, f"{controller.name}.startup"), "w", encoding="utf-8"
    ) as f:
        f.write("\n".join(controller.cmd_list))

    return out_path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate Kathara lab config for SDN Clos"
    )
    parser.add_argument(
        "-s", "--size", choices=["s", "m", "l"], default="s", help="Topology size"
    )
    parser.add_argument("-o", "--output", default=None, help="Output directory")
    args = parser.parse_args()
    out = generate_sdn_clos_topology(
        topo_size=args.size,
        output_dir=args.output or os.path.join(SCRIPT_DIR, "topology"),
    )
    print(f"Lab configuration generated at: {out}")
