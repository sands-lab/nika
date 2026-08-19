#!/usr/bin/env python3
"""
Standalone lab generator for P4 INT (In-band Network Telemetry) topology.
Generates Kathara-compatible lab configuration WITHOUT Kathara dependency.
Output: topology/ folder containing lab.conf, *.startup, and config files.
No topology size input - fixed topology (2 hosts, 2 spine, 2 leaf, 1 collector).
"""

import os
from dataclasses import dataclass, field

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR))))
)
P4_UTILS = os.path.join(PROJECT_ROOT, "src/nika/net_env/kathara/utils/p4")


@dataclass
class MachineMeta:
    name: str
    eth_index: int = 0
    cmd_list: list[str] = field(default_factory=list)
    image: str = "kathara/p4"
    cpus: float = 0.5
    mem: str = "256m"
    links: list[tuple[int, str]] = field(default_factory=list)
    extra_files: dict[str, str] = field(default_factory=dict)


def _read_file(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _copy_dir_to_extra_files(dir_path: str, prefix: str = "") -> dict[str, str]:
    result = {}
    if not os.path.isdir(dir_path):
        return result
    for root, _dirs, files in os.walk(dir_path):
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, dir_path)
            key = prefix + rel
            result[key] = _read_file(full)
    return result


def generate_p4_int_topology(output_dir: str | None = None) -> str:
    """
    Generate Kathara-compatible lab configuration for P4 INT.
    Returns the absolute path to the output directory.
    """
    if output_dir is None:
        output_dir = os.path.join(SCRIPT_DIR, "topology")

    pc1 = MachineMeta(name="pc1", image="kathara/base")
    pc2 = MachineMeta(name="pc2", image="kathara/base")
    collector = MachineMeta(name="collector", image="nika/influxdb")
    spine1 = MachineMeta(name="spine1", image="kathara/p4")
    spine2 = MachineMeta(name="spine2", image="kathara/p4")
    leaf1 = MachineMeta(name="leaf1", image="kathara/p4")
    leaf2 = MachineMeta(name="leaf2", image="kathara/p4")

    # Topology from lab.py
    pc1.links.append((0, "A"))
    leaf1.links.append((0, "A"))
    leaf1.links.append((1, "B"))
    spine1.links.append((0, "B"))
    leaf1.links.append((2, "C"))
    spine2.links.append((0, "C"))
    pc2.links.append((0, "D"))
    leaf2.links.append((0, "D"))
    leaf2.links.append((1, "E"))
    spine1.links.append((1, "E"))
    leaf2.links.append((2, "F"))
    spine2.links.append((1, "F"))
    collector.links.append((0, "G"))
    leaf2.links.append((3, "G"))

    # Host startup
    for i, host in enumerate([pc1, pc2], start=1):
        cmd_list = [
            "sysctl net.ipv4.conf.all.arp_ignore=8",
            "sysctl net.ipv4.conf.default.arp_ignore=8",
            "sysctl net.ipv4.conf.all.arp_announce=8",
            "sysctl net.ipv4.conf.default.arp_announce=8",
            f"ip link set eth0 address 00:00:0a:00:00:0{i}",
            f"ip addr add 10.0.0.{i}/24 dev eth0",
        ]
        for j in range(1, 4):  # 2 hosts + collector
            if j != i:
                cmd_list.append(f"arp -s 10.0.0.{j} 00:00:0a:00:00:0{j}")
        host.cmd_list = cmd_list

    # Collector startup
    collector.cmd_list = [
        "sysctl net.ipv4.conf.all.arp_ignore=8",
        "sysctl net.ipv4.conf.default.arp_ignore=8",
        "sysctl net.ipv4.conf.all.arp_announce=8",
        "sysctl net.ipv4.conf.default.arp_announce=8",
        "ip link set eth0 address 00:00:0a:00:00:03",
        "ip addr add 10.0.0.3/24 dev eth0",
        "arp -s 10.0.0.1 00:00:0a:00:00:01",
        "arp -s 10.0.0.2 00:00:0a:00:00:02",
        "python3 collector_src/int_collector.py &> int_collector.log",
    ]
    collector.extra_files = _copy_dir_to_extra_files(
        os.path.join(SCRIPT_DIR, "collector_src"), "collector_src/"
    )

    # Switch files and startup
    switches = [spine1, spine2, leaf1, leaf2]
    intf_map = {"spine1": 2, "spine2": 2, "leaf1": 4, "leaf2": 4}
    for sw in switches:
        sw.extra_files = _copy_dir_to_extra_files(
            os.path.join(SCRIPT_DIR, "p4_src"), "p4_src/"
        )
        sw.extra_files["commands.txt"] = _read_file(
            os.path.join(SCRIPT_DIR, f"cmds/{sw.name}.txt")
        )
        if os.path.exists(os.path.join(P4_UTILS, "sswitch_thrift_API.py")):
            sw.extra_files[
                "usr/local/lib/python3.11/site-packages/sswitch_thrift_API.py"
            ] = _read_file(os.path.join(P4_UTILS, "sswitch_thrift_API.py"))
        if os.path.exists(os.path.join(P4_UTILS, "thrift_API.py")):
            sw.extra_files["usr/local/lib/python3.11/site-packages/thrift_API.py"] = (
                _read_file(os.path.join(P4_UTILS, "thrift_API.py"))
            )
        intf_num = intf_map[sw.name]
        intf_str = " ".join(f"-i {i + 1}@eth{i}" for i in range(intf_num))
        sw.cmd_list = [
            "sysctl net.ipv4.conf.all.arp_ignore=8",
            "sysctl net.ipv4.conf.default.arp_ignore=8",
            "sysctl net.ipv4.conf.all.arp_announce=8",
            "sysctl net.ipv4.conf.default.arp_announce=8",
            "p4c p4_src/int.p4 -o p4_src/",
            f"simple_switch {intf_str} --log-console p4_src/int.json >> sw.log &",
            "while [[ $(pgrep simple_switch) -eq 0 ]]; do sleep 1; done",
            'until simple_switch_CLI <<< "help"; do sleep 1; done',
            "simple_switch_CLI <<< $(cat commands.txt)",
        ]

    all_machines = [pc1, pc2, collector, spine1, spine2, leaf1, leaf2]
    return _write_lab(
        output_dir, all_machines, "p4_int", "P4 INT - 2 hosts, 4 switches, 1 collector"
    )


def _write_lab(
    out_path: str,
    all_machines: list[MachineMeta],
    lab_name: str,
    lab_description: str,
) -> str:
    out_path = os.path.abspath(out_path)
    os.makedirs(out_path, exist_ok=True)

    lab_conf_lines = [
        f'LAB_NAME="{lab_name}"',
        f'LAB_DESCRIPTION="{lab_description}"',
        "",
    ]
    for meta in all_machines:
        for eth_idx, collision_domain in meta.links:
            lab_conf_lines.append(f'{meta.name}[{eth_idx}]="{collision_domain}"')
        lab_conf_lines.append(f'{meta.name}[image]="{meta.image}"')
        lab_conf_lines.append(f"{meta.name}[cpus]={meta.cpus}")
        lab_conf_lines.append(f'{meta.name}[mem]="{meta.mem}"')
        lab_conf_lines.append("")

    with open(os.path.join(out_path, "lab.conf"), "w", encoding="utf-8") as f:
        f.write("\n".join(lab_conf_lines))

    for meta in all_machines:
        startup_path = os.path.join(out_path, f"{meta.name}.startup")
        with open(startup_path, "w", encoding="utf-8") as f:
            f.write("\n".join(meta.cmd_list))
        if meta.extra_files:
            machine_dir = os.path.join(out_path, meta.name)
            for file_path, content in meta.extra_files.items():
                full_path = os.path.join(machine_dir, file_path.lstrip("/"))
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(content)

    return out_path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate Kathara lab config for P4 INT"
    )
    parser.add_argument(
        "-o", "--output", default=None, help="Output directory (default: topology/)"
    )
    args = parser.parse_args()
    out = generate_p4_int_topology(
        output_dir=args.output or os.path.join(SCRIPT_DIR, "topology")
    )
    print(f"Lab configuration generated at: {out}")
