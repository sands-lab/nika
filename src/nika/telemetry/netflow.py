"""NetFlow/IPFIX telemetry: pmacct probes on capable devices, nfdump collector.

Two phases: ``attach`` wires the collector and the export LAN before deployment;
``configure`` runs in the lab check phase, probes each container for pmacct and
configures only the devices that have it.
"""

from __future__ import annotations

import ipaddress
import re
from typing import TYPE_CHECKING, Any

from nika.net_env.utils.kathara.docker_files.docker_images import nika_image
from nika.net_env.verify import exec_or_empty, process_running
from nika.runtime.spec import NodeRole
from nika.telemetry import NETFLOW, TELEMETRY_NETWORK
from nika.utils.parallel import bounded_parallel_map

if TYPE_CHECKING:
    from Kathara.model.Lab import Lab

    from nika.net_env.base import NetworkEnvBase
    from nika.runtime.base import LabRuntime

FLOW_COLLECTOR = "flow_collector"
IPFIX_PORT = 9995
FLOW_DIR = "/var/lib/nika/flows"
FLOW_EXPORTERS_FILE = "/var/lib/nika/flow_exporters"
# nfcapd rotation period; nfdump reads closed files, so this bounds query lag.
FLOW_ROTATE_SEC = 10
# pmacct nfprobe timeouts: export active flows every 10 s, idle flows after 10 s,
# and check for expired flows every 2 s.
NFPROBE_TIMEOUTS = (
    "general=10:maxlife=10:udp=10:icmp=10:tcp=10:tcp.fin=5:tcp.rst=5:expint=2"
)

PMACCT_DIR = "/etc/pmacct"
PMACCT_SCRIPT = f"{PMACCT_DIR}/nika-netflow.sh"
# Routing table of each exporter's ``TELEMETRY_NETWORK`` VRF.
EXPORT_VRF_TABLE = 4242
# Policy rule, ahead of the main table, that sends probe exports into the VRF.
EXPORT_RULE_PREF = 100
# ``pmacctd -V`` prints e.g. "Promiscuous Mode Accounting Daemon, pmacctd 1.7.7-git".
_PMACCTD_VERSION = re.compile(r"\bpmacctd (\d[\w.\-]*)")


def export_plan(subnet: str, exporters: list[str]) -> tuple[str, int, dict[str, str]]:
    """Return the collector address, prefix length, and exporter addresses.

    Exporters take the first hosts in name order; the collector takes the last.
    """
    network = ipaddress.ip_network(subnet)
    # Usable hosts exclude the network and broadcast addresses and the collector.
    room = network.num_addresses - 3
    if len(exporters) > room:
        raise ValueError(
            f"NetFlow subnet {subnet} has room for {room} exporters, "
            f"but {len(exporters)} devices support NetFlow."
        )
    addresses = {
        name: str(network[index]) for index, name in enumerate(sorted(exporters), 1)
    }
    return str(network[-2]), network.prefixlen, addresses


def pmacct_conf_path(iface: str) -> str:
    return f"{PMACCT_DIR}/nika-netflow-{iface}.conf"


def pmacct_conf(collector: str, iface: str, engine: int) -> str:
    """Ingress-only IPFIX probe for one interface.

    One probe per interface: a pmacctd capturing several interfaces stops
    exporting all of them once one goes down. ``in_if`` is the kernel ifindex,
    and ``engine`` gives each probe its own IPFIX observation domain.
    """
    return "\n".join(
        [
            "daemonize: false",
            f"pcap_interface: {iface}",
            "pcap_ifindex: sys",
            "pcap_direction: in",
            "pcap_filter: ip",
            "plugins: nfprobe",
            f"nfprobe_receiver: {collector}:{IPFIX_PORT}",
            "nfprobe_version: 10",
            f"nfprobe_engine: {engine}",
            f"nfprobe_timeouts: {NFPROBE_TIMEOUTS}",
            "aggregate: src_host,dst_host,src_port,dst_port,proto,tos,in_iface",
            "",
        ]
    )


def exporter_script(
    *,
    data_ifaces: list[str],
    export_iface: str,
    address: str,
    prefixlen: int,
    collector: str,
) -> str:
    """Isolate the export interface in a VRF, then run one probe per data interface.

    The VRF keeps the export LAN out of the device's routing protocols. The
    probes reach the collector through a policy rule into the VRF table rather
    than a route in the main table, which ``redistribute connected`` or
    ``redistribute kernel`` would advertise.
    """
    probes = [
        # pmacctd cannot open a down interface, so wait for it; a running probe
        # pauses while its link is down and resumes when it comes back.
        f"(while true; do until grep -qx up /sys/class/net/{iface}/operstate "
        f"2>/dev/null; do sleep 1; done; pmacctd -f {pmacct_conf_path(iface)}; "
        "sleep 1; done) &"
        for iface in data_ifaces
    ]
    return "\n".join(
        [
            "#!/bin/sh",
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
            # The export LAN is attached after the scenario links; retry until it is up.
            'i=0; until setup; do i=$((i+1)); [ "$i" -ge 60 ] && '
            '{ echo "NetFlow export interface setup failed" >&2; exit 1; }; '
            "sleep 1; done",
            *probes,
            "wait",
            "",
        ]
    )


def collector_startup(collector: str, prefixlen: int, export_iface: str) -> list[str]:
    return [
        f"ip addr add {collector}/{prefixlen} dev {export_iface}",
        f"ip link set {export_iface} up",
        f"mkdir -p {FLOW_DIR}",
        f"nfcapd -D -w {FLOW_DIR} -t {FLOW_ROTATE_SEC} -p {IPFIX_PORT} "
        "-P /run/nfcapd.pid",
    ]


def attach(net_env: NetworkEnvBase, subnet: str) -> dict[str, Any]:
    """Wire the collector and the export LAN into the built, not yet deployed, lab.

    Every device joins the export LAN on its last interface; none is configured
    yet. ``configure`` later sets up only the devices whose container runs pmacct.
    """
    lab = net_env.lab
    devices = sorted(lab.machines)
    lab.new_machine(
        FLOW_COLLECTOR, image=nika_image("flow-collector"), cpus=0.5, mem="256m"
    )
    net_env.declare_machine(
        FLOW_COLLECTOR,
        role=NodeRole.INFRASTRUCTURE,
        capabilities=("linux", NETFLOW),
    )
    collector_ip, prefixlen, _ = export_plan(subnet, [])
    collector_iface = _connect_last(lab, FLOW_COLLECTOR)
    lab.create_file_from_list(
        collector_startup(collector_ip, prefixlen, collector_iface),
        f"{FLOW_COLLECTOR}.startup",
    )
    candidates = {}
    for name in devices:
        data_ifaces = [f"eth{num}" for num in sorted(lab.machines[name].interfaces)]
        candidates[name] = {
            "export_iface": _connect_last(lab, name),
            "data_ifaces": data_ifaces,
        }
    net_env.load_machines()
    return {
        "collector": FLOW_COLLECTOR,
        "subnet": subnet,
        "candidates": candidates,
        "exporters": {},
        "versions": {},
    }


def _connect_last(lab: Lab, name: str) -> str:
    """Connect ``name`` to the export LAN on the interface after its last one.

    Kathara would otherwise take the first free number, which fills a gap in a
    scenario's interface numbering instead of coming last. On ``bridged``
    devices, Kathara appends its host-bridge NIC after it at deploy time.
    """
    number = max(lab.machines[name].interfaces, default=-1) + 1
    lab.connect_machine_to_link(name, TELEMETRY_NETWORK, machine_iface_number=number)
    return f"eth{number}"


def probe_versions(runtime: LabRuntime, devices: list[str]) -> dict[str, str]:
    """Return the pmacct version installed in each device that has one."""

    def probe(name: str) -> str | None:
        out = exec_or_empty(runtime, name, "pmacctd -V 2>&1 | head -n 1")
        match = _PMACCTD_VERSION.search(out)
        return match.group(1) if match else None

    found = bounded_parallel_map(probe, devices)
    return {
        name: version for name, version in zip(devices, found, strict=True) if version
    }


def configure(runtime: LabRuntime, plan: dict[str, Any]) -> dict[str, Any]:
    """Configure export on every device whose container runs pmacct.

    Devices without pmacct keep their export interface unconfigured. With no
    capable device, the collector simply receives no records.
    """
    versions = probe_versions(runtime, sorted(plan["candidates"]))
    collector_ip, prefixlen, addresses = export_plan(plan["subnet"], list(versions))

    def start(name: str) -> None:
        device = plan["candidates"][name]
        runtime.exec(name, f"mkdir -p {PMACCT_DIR}")
        for engine, iface in enumerate(device["data_ifaces"], 1):
            runtime.write_file(
                name, pmacct_conf_path(iface), pmacct_conf(collector_ip, iface, engine)
            )
        runtime.write_file(
            name,
            PMACCT_SCRIPT,
            exporter_script(
                data_ifaces=device["data_ifaces"],
                export_iface=device["export_iface"],
                address=addresses[name],
                prefixlen=prefixlen,
                collector=collector_ip,
            ),
        )
        runtime.exec(
            name, f"nohup sh {PMACCT_SCRIPT} >/var/log/nika-netflow.log 2>&1 &"
        )

    bounded_parallel_map(start, sorted(addresses))
    runtime.write_file(
        FLOW_COLLECTOR,
        FLOW_EXPORTERS_FILE,
        "".join(f"{ip} {name}\n" for name, ip in addresses.items()),
    )
    return {**plan, "exporters": addresses, "versions": versions}


def export_running(runtime: LabRuntime, metadata: dict[str, Any]) -> bool:
    """The collector and every exporter probe are running."""
    return process_running(runtime, FLOW_COLLECTOR, "nfcapd") and all(
        process_running(runtime, name, "pmacctd") for name in metadata["exporters"]
    )
