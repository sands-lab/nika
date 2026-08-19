#!/usr/bin/env python3
"""
Standalone lab generator for Simple BGP topology.
Generates Kathara-compatible lab configuration WITHOUT Kathara dependency.
Output: topology/ folder containing lab.conf, *.startup, and host config subfolders.
"""

import os
from dataclasses import dataclass, field

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR))))
)
BGP_UTILS = os.path.join(PROJECT_ROOT, "src/nika/net_env/kathara/utils/bgp")


@dataclass
class MachineMeta:
    name: str
    eth_index: int = 0
    cmd_list: list[str] = field(default_factory=list)
    image: str = "nika/frr"
    cpus: float = 1.0
    mem: str = "256m"
    links: list[tuple[int, str]] = field(default_factory=list)
    extra_files: dict[str, str] = field(default_factory=dict)


def _read_file(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def generate_simple_bgp_topology(
    output_dir: str | None = None, bgp_utils_path: str | None = None
) -> str:
    """
    Generate Kathara-compatible lab configuration for Simple BGP (2 routers, 2 hosts).
    Returns the absolute path to the output directory.
    """
    if output_dir is None:
        output_dir = os.path.join(SCRIPT_DIR, "topology")
    bgp_utils = bgp_utils_path or BGP_UTILS

    router1 = MachineMeta(name="router1", image="nika/frr", cpus=1.0)
    router2 = MachineMeta(name="router2", image="nika/frr", cpus=1.0)
    pc1 = MachineMeta(name="pc1", image="nika/base")
    pc2 = MachineMeta(name="pc2", image="nika/base")

    # Link A: router1 -- router2
    router1.links.append((0, "A"))
    router2.links.append((0, "A"))
    # Link B: router1 -- pc1
    router1.links.append((1, "B"))
    pc1.links.append((0, "B"))
    # Link C: router2 -- pc2
    router2.links.append((1, "C"))
    pc2.links.append((0, "C"))

    # Startup and FRR from existing files
    router1.cmd_list = (
        _read_file(os.path.join(SCRIPT_DIR, "router1.startup")).strip().split("\n")
    )
    router2.cmd_list = (
        _read_file(os.path.join(SCRIPT_DIR, "router2.startup")).strip().split("\n")
    )
    pc1.cmd_list = (
        _read_file(os.path.join(SCRIPT_DIR, "pc1.startup")).strip().split("\n")
    )
    pc2.cmd_list = (
        _read_file(os.path.join(SCRIPT_DIR, "pc2.startup")).strip().split("\n")
    )

    for i, router in enumerate([router1, router2], start=1):
        router.extra_files["/etc/frr/daemons"] = _read_file(
            os.path.join(bgp_utils, "daemons")
        )
        router.extra_files["/etc/frr/vtysh.conf"] = _read_file(
            os.path.join(bgp_utils, "vtysh.conf")
        )
        router.extra_files["/etc/frr/frr.conf"] = _read_file(
            os.path.join(SCRIPT_DIR, f"router{i}/etc/frr/frr.conf")
        )

    all_machines = [router1, router2, pc1, pc2]
    return _write_lab(
        output_dir, all_machines, "simple_bgp", "Simple BGP - 2 routers, 2 hosts"
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
            host_dir = os.path.join(out_path, meta.name)
            for file_path, content in meta.extra_files.items():
                rel_path = file_path.lstrip("/")
                full_path = os.path.join(host_dir, rel_path)
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(content)

    return out_path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate Kathara lab config for Simple BGP"
    )
    parser.add_argument(
        "-o", "--output", default=None, help="Output directory (default: topology/)"
    )
    args = parser.parse_args()
    out = generate_simple_bgp_topology(
        output_dir=args.output or os.path.join(SCRIPT_DIR, "topology")
    )
    print(f"Lab configuration generated at: {out}")
