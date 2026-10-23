"""Out-of-band management network shared by every telemetry kind.

Two phases: ``attach`` wires the collector and the management LAN before
deployment; ``configure`` runs in the lab check phase. It probes every
container for each requested kind, gives a management address to each device
that supports at least one, and starts that kind's agents there and its
services on the collector. A kind no device supports is not an error.

Each kind module (``netflow``, ``prometheus``) provides ``probe``,
``device_files``, ``device_lines``, ``collector_files``, ``collector_lines``
and ``running``.
"""

from __future__ import annotations

import ipaddress
import posixpath
from typing import TYPE_CHECKING, Any

from nika.net_env.utils.kathara.docker_files.docker_images import nika_image
from nika.runtime.spec import NodeRole
from nika.telemetry import (
    DEVICE_EXEC_WORKERS,
    NETFLOW,
    PROMETHEUS,
    TELEMETRY_NETWORK,
    netflow,
    prometheus,
    telemetry_link,
)
from nika.utils.parallel import bounded_parallel_map

if TYPE_CHECKING:
    from Kathara.model.Lab import Lab

    from nika.net_env.base import NetworkEnvBase
    from nika.runtime.base import LabRuntime

KINDS = {NETFLOW: netflow, PROMETHEUS: prometheus}
TELEMETRY_COLLECTOR = "telemetry_collector"
TELEMETRY_SWITCH = "telemetry_switch"
# Routing table of each device's ``TELEMETRY_NETWORK`` VRF.
EXPORT_VRF_TABLE = 4242
# Policy rule, ahead of the main table, that sends collector traffic into the VRF.
EXPORT_RULE_PREF = 100
SCRIPT_DIR = "/etc/nika-telemetry"
SCRIPT = f"{SCRIPT_DIR}/start.sh"
LOG = "/var/log/nika-telemetry.log"


def address_plan(subnet: str, devices: list[str]) -> tuple[str, int, dict[str, str]]:
    """Return the collector address, prefix length, and device addresses.

    Devices take the first hosts in name order; the collector takes the last.
    """
    network = ipaddress.ip_network(subnet)
    # Usable hosts exclude the network and broadcast addresses and the collector.
    room = network.num_addresses - 3
    if len(devices) > room:
        raise ValueError(
            f"Telemetry subnet {subnet} has room for {room} devices, "
            f"but {len(devices)} devices support telemetry."
        )
    addresses = {
        name: str(network[index]) for index, name in enumerate(sorted(devices), 1)
    }
    return str(network[-2]), network.prefixlen, addresses


def attach(net_env: NetworkEnvBase, subnet: str, kinds: tuple[str, ...]) -> dict:
    """Wire the collector and the management LAN into the built, not yet deployed, lab.

    The LAN is a star: ``telemetry_switch`` bridges, in its kernel, one
    point-to-point link per device and one to the collector. Kathara links are
    userspace hubs that copy every frame to every port, so one shared link
    would not scale to large labs. Every device joins on its last interface;
    none is configured yet.
    """
    lab = net_env.lab
    devices = sorted(lab.machines)
    _add_node(net_env, TELEMETRY_SWITCH, nika_image("base"), capabilities=("linux",))
    _add_node(
        net_env,
        TELEMETRY_COLLECTOR,
        nika_image("telemetry-collector"),
        capabilities=("linux", *kinds),
        # Prometheus holds every device's series in memory (~330 MB for 322).
        mem="1g",
    )
    collector_ip, prefixlen, _ = address_plan(subnet, [])
    collector_iface = _connect_last(lab, TELEMETRY_COLLECTOR)
    lab.create_file_from_list(
        [
            f"ip addr add {collector_ip}/{prefixlen} dev {collector_iface}",
            f"ip link set {collector_iface} up",
        ],
        f"{TELEMETRY_COLLECTOR}.startup",
    )
    plan_devices = {}
    for name in devices:
        data_ifaces = [f"eth{num}" for num in sorted(lab.machines[name].interfaces)]
        plan_devices[name] = {
            "export_iface": _connect_last(lab, name),
            "data_ifaces": data_ifaces,
        }
    lab.create_file_from_list(
        [
            f"ip link add {TELEMETRY_NETWORK} type bridge stp_state 0",
            f"for intf in $(ls /sys/class/net | grep '^eth'); do "
            f"ip link set $intf master {TELEMETRY_NETWORK}; ip link set $intf up; done",
            f"ip link set {TELEMETRY_NETWORK} up",
        ],
        f"{TELEMETRY_SWITCH}.startup",
    )
    net_env.load_machines()
    return {
        "collector": TELEMETRY_COLLECTOR,
        "switch": TELEMETRY_SWITCH,
        "subnet": subnet,
        "devices": plan_devices,
        "addresses": {},
        "kinds": {kind: {"versions": {}} for kind in kinds},
    }


def _add_node(
    net_env: NetworkEnvBase,
    name: str,
    image: str,
    *,
    capabilities: tuple[str, ...],
    mem: str = "256m",
) -> None:
    net_env.lab.new_machine(name, image=image, cpus=0.5, mem=mem)
    net_env.declare_machine(
        name, role=NodeRole.INFRASTRUCTURE, capabilities=capabilities
    )


def _connect_last(lab: Lab, name: str) -> str:
    """Link ``name`` to the switch on the interface after its last one.

    Kathara would otherwise take the first free number, which fills a gap in a
    scenario's interface numbering instead of coming last. On ``bridged``
    devices, Kathara appends its host-bridge NIC after it at deploy time.
    """
    link = telemetry_link(name)
    number = max(lab.machines[name].interfaces, default=-1) + 1
    lab.connect_machine_to_link(name, link, machine_iface_number=number)
    lab.connect_machine_to_link(TELEMETRY_SWITCH, link)
    return f"eth{number}"


def setup_lines(
    *, export_iface: str, address: str, prefixlen: int, collector: str
) -> list[str]:
    """Isolate the management interface in a VRF reachable from the collector.

    The VRF keeps the management LAN out of the device's routing protocols.
    Traffic to the collector reaches it through a policy rule into the VRF
    table rather than a route in the main table, which ``redistribute
    connected`` or ``redistribute kernel`` would advertise.
    """
    return [
        "setup() {",
        f"  ip link show {TELEMETRY_NETWORK} >/dev/null 2>&1 || "
        f"ip link add {TELEMETRY_NETWORK} type vrf table {EXPORT_VRF_TABLE} "
        "|| return 1",
        f"  ip link set {TELEMETRY_NETWORK} up &&",
        f"  ip link set {export_iface} master {TELEMETRY_NETWORK} &&",
        f"  ip link set {export_iface} up &&",
        f"  ip addr replace {address}/{prefixlen} dev {export_iface} &&",
        f"  {{ ip rule | grep -q 'to {collector} lookup {EXPORT_VRF_TABLE}' || "
        f"ip rule add to {collector}/32 lookup {EXPORT_VRF_TABLE} "
        f"pref {EXPORT_RULE_PREF}; }}",
        "}",
        'i=0; until setup; do i=$((i+1)); [ "$i" -ge 60 ] && '
        '{ echo "Telemetry management interface setup failed" >&2; exit 1; }; '
        "sleep 1; done",
    ]


def configure(runtime: LabRuntime, plan: dict[str, Any]) -> dict[str, Any]:
    """Configure every requested kind on the devices whose container supports it."""
    kinds = sorted(plan["kinds"])
    pairs = [(name, kind) for name in sorted(plan["devices"]) for kind in kinds]
    found = bounded_parallel_map(
        lambda pair: KINDS[pair[1]].probe(runtime, pair[0]),
        pairs,
        max_workers=DEVICE_EXEC_WORKERS,
    )
    versions: dict[str, dict[str, str]] = {kind: {} for kind in kinds}
    for (name, kind), version in zip(pairs, found, strict=True):
        if version:
            versions[kind][name] = version
    configured = sorted({name for names in versions.values() for name in names})
    collector_ip, prefixlen, addresses = address_plan(plan["subnet"], configured)

    def start(name: str) -> None:
        device = plan["devices"][name]
        own = [KINDS[kind] for kind in kinds if name in versions[kind]]
        files: dict[str, str] = {}
        for kind in own:
            files.update(kind.device_files(device, addresses[name], collector_ip))
        lines = setup_lines(
            export_iface=device["export_iface"],
            address=addresses[name],
            prefixlen=prefixlen,
            collector=collector_ip,
        )
        for kind in own:
            lines += kind.device_lines(device, addresses[name])
        _run(runtime, name, files, lines)

    bounded_parallel_map(start, configured, max_workers=DEVICE_EXEC_WORKERS)
    files: dict[str, str] = {}
    lines: list[str] = []
    for kind in kinds:
        targets = {name: addresses[name] for name in versions[kind]}
        files.update(KINDS[kind].collector_files(targets))
        lines += KINDS[kind].collector_lines()
    _run(runtime, plan["collector"], files, lines)
    return {
        **plan,
        "addresses": addresses,
        "kinds": {kind: {"versions": versions[kind]} for kind in kinds},
    }


def _run(
    runtime: LabRuntime, name: str, files: dict[str, str], lines: list[str]
) -> None:
    """Write ``files`` and a start script on ``name``, then run it detached."""
    dirs = sorted({SCRIPT_DIR, *(posixpath.dirname(path) for path in files)})
    runtime.exec(name, f"mkdir -p {' '.join(dirs)}")
    for path, content in files.items():
        runtime.write_file(name, path, content)
    runtime.write_file(name, SCRIPT, "\n".join(["#!/bin/sh", *lines, "wait", ""]))
    runtime.exec(name, f"nohup sh {SCRIPT} >{LOG} 2>&1 &")


def running(runtime: LabRuntime, plan: dict[str, Any]) -> bool:
    """Every requested kind has its collector service and device agents running."""
    return all(
        KINDS[kind].running(
            runtime,
            plan["collector"],
            {name: plan["addresses"][name] for name in info["versions"]},
        )
        for kind, info in plan["kinds"].items()
    )
