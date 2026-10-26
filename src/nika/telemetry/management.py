"""Out-of-band management network shared by every telemetry kind.

Two phases: ``attach`` wires the collector and the management LAN before
deployment; ``configure`` runs in the lab check phase. It probes every
container for each requested kind, gives a management address to each device
that supports at least one, and starts that kind's exporters there and its
services on the collector. A kind no device supports is not an error.

Each kind module (``netflow``, ``prometheus``, ``syslog``) provides ``probe``,
``device_files``, ``device_lines``, ``collector_files``, ``collector_lines``
and ``running``. ``device_files`` / ``device_lines`` take the probed
``version`` so a kind can pick pmacct vs OVS NetFlow, or node_exporter vs
OpenFlow vs P4 counters.
"""

from __future__ import annotations

import ipaddress
import posixpath
from typing import TYPE_CHECKING, Any

from nika.net_env.utils.kathara.docker_files.docker_images import nika_image
from nika.runtime.spec import NodeRole
from nika.telemetry import (
    CONFIG_DIR,
    DEVICE_EXEC_WORKERS,
    MGMT_VRF,
    NETFLOW,
    PROMETHEUS,
    SYSLOG,
    netflow,
    prometheus,
    syslog,
    telemetry_link,
)
from nika.utils.parallel import bounded_parallel_map

if TYPE_CHECKING:
    from Kathara.model.Lab import Lab

    from nika.net_env.base import NetworkEnvBase
    from nika.runtime.base import LabRuntime

KINDS = {NETFLOW: netflow, PROMETHEUS: prometheus, SYSLOG: syslog}
TELEMETRY_COLLECTOR = "telemetry_collector"
TELEMETRY_SWITCH = "telemetry_switch"
# Routing table of each device's ``MGMT_VRF``.
MGMT_VRF_TABLE = 4242
# Policy rule, ahead of the main table, that sends collector traffic into the VRF.
MGMT_RULE_PREF = 100
# Bridge on the switch that joins its management links.
SWITCH_BRIDGE = "br0"
SCRIPT = f"{CONFIG_DIR}/start.sh"
LOG = "/var/log/telemetry.log"


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


def attach(
    net_env: NetworkEnvBase, subnet: str, kinds: tuple[str, ...]
) -> dict[str, Any]:
    """Wire the collector and the management LAN into the built, not yet deployed, lab.

    The LAN is a star: ``telemetry_switch`` bridges, in its kernel, one
    point-to-point link per device and one to the collector. Kathara links are
    userspace hubs that copy every frame to every port, so one shared link
    would not scale to large labs. Every device joins on an interface after
    its scenario interfaces; none is configured yet.
    """
    lab = net_env.lab
    devices = _exporting_devices(net_env)
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
            "mgmt_iface": _connect_last(lab, name),
            "data_ifaces": data_ifaces,
        }
    lab.create_file_from_list(
        [
            f"ip link add {SWITCH_BRIDGE} type bridge stp_state 0",
            f"for intf in $(ls /sys/class/net | grep '^eth'); do "
            f"ip link set $intf master {SWITCH_BRIDGE}; ip link set $intf up; done",
            f"ip link set {SWITCH_BRIDGE} up",
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


def _exporting_devices(net_env: NetworkEnvBase) -> list[str]:
    """Devices that export telemetry. Skip INT-MX sinks.

    A node whose capabilities are only ``linux`` and ``telemetry`` receives
    hop reports; it does not export NetFlow, Prometheus, or syslog.
    """
    names: list[str] = []
    for name in sorted(net_env.lab.machines):
        identity = net_env.machine_identities.get(name)
        caps = set(identity.capabilities) if identity else set()
        if caps <= {"linux", "telemetry"} and "telemetry" in caps:
            continue
        names.append(name)
    return names


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
    *, mgmt_iface: str, address: str, prefixlen: int, collector: str
) -> list[str]:
    """Isolate the management interface in a VRF reachable from the collector.

    The VRF keeps the management LAN out of the device's routing protocols.
    Traffic to the collector reaches it through a policy rule into the VRF
    table rather than a route in the main table, which ``redistribute
    connected`` or ``redistribute kernel`` would advertise.
    """
    return [
        "setup() {",
        f"  ip link show {MGMT_VRF} >/dev/null 2>&1 || "
        f"ip link add {MGMT_VRF} type vrf table {MGMT_VRF_TABLE} "
        "|| return 1",
        f"  ip link set {MGMT_VRF} up &&",
        f"  ip link set {mgmt_iface} master {MGMT_VRF} &&",
        f"  ip link set {mgmt_iface} up &&",
        f"  ip addr replace {address}/{prefixlen} dev {mgmt_iface} &&",
        f"  {{ ip rule | grep -q 'to {collector} lookup {MGMT_VRF_TABLE}' || "
        f"ip rule add to {collector}/32 lookup {MGMT_VRF_TABLE} "
        f"pref {MGMT_RULE_PREF}; }}",
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
        from nika.telemetry import routeros

        device = {**plan["devices"][name], "prefixlen": prefixlen}
        own = [
            (KINDS[kind], versions[kind][name])
            for kind in kinds
            if name in versions[kind]
        ]
        files: dict[str, str] = {}
        for kind, version in own:
            files.update(
                kind.device_files(
                    device, addresses[name], collector_ip, version=version
                )
            )
        if any(routeros.is_ros(version) for _, version in own):
            flow_dest = ""
            if NETFLOW in versions and name in versions[NETFLOW]:
                flow_dest = collector_ip
            files[routeros.SETUP_SCRIPT] = routeros.setup_script(
                device, addresses[name], collector=flow_dest
            )
            lines = [f"sh {routeros.SETUP_SCRIPT}"]
        else:
            lines = setup_lines(
                mgmt_iface=device["mgmt_iface"],
                address=addresses[name],
                prefixlen=prefixlen,
                collector=collector_ip,
            )
        for kind, version in own:
            lines += kind.device_lines(device, addresses[name], version=version)
        _run(runtime, name, files, lines)

    bounded_parallel_map(start, configured, max_workers=DEVICE_EXEC_WORKERS)
    files: dict[str, str] = {}
    lines: list[str] = []
    from nika.telemetry import routeros

    ros_hidden = ",".join(
        sorted(
            {
                routeros.linux_to_ether(plan["devices"][name]["mgmt_iface"])
                for name, version in versions.get(PROMETHEUS, {}).items()
                if routeros.is_ros(version)
            }
        )
    )
    for kind in kinds:
        targets = {name: addresses[name] for name in versions[kind]}
        files.update(KINDS[kind].collector_files(targets, versions=versions[kind]))
        extra = {"hidden": ros_hidden} if kind == PROMETHEUS and ros_hidden else {}
        lines += KINDS[kind].collector_lines(targets, versions=versions[kind], **extra)
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
    dirs = sorted({CONFIG_DIR, *(posixpath.dirname(path) for path in files)})
    runtime.exec(name, f"mkdir -p {' '.join(dirs)}")
    for path, content in files.items():
        runtime.write_file(name, path, content)
    runtime.write_file(name, SCRIPT, "\n".join(["#!/bin/sh", *lines, "wait", ""]))
    runtime.exec(name, f"nohup sh {SCRIPT} >{LOG} 2>&1 &")


def running(runtime: LabRuntime, plan: dict[str, Any]) -> bool:
    """Every requested kind has its collector service and device exporters running."""
    return all(
        KINDS[kind].running(
            runtime,
            plan["collector"],
            {name: plan["addresses"][name] for name in info["versions"]},
        )
        for kind, info in plan["kinds"].items()
    )
