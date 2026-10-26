"""NetFlow/IPFIX/sFlow telemetry: pmacct, OVS NetFlow, SR Linux sFlow, or RouterOS.

A telemetry kind for ``nika.telemetry.management``, which wires the shared
management network and calls these functions.

FRR images export IPFIX with pmacct. OVS switches (``kathara/sdn``) export
NetFlow v5 from the datapath bridge, which is the forwarding path in SDN labs.
OVS IPFIX templates land with epoch timestamps that nfdump's query window
drops, so SDN uses NetFlow v5. Containerlab SR Linux has no IPFIX; it exports
sFlow over a dedicated ip-vrf to ``sfcapd`` on the collector. Kathara
RouterOS exports NetFlow v9 from traffic-flow on the extra management ether.
XRd Control Plane has no collector-reachable flow export.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from nika.net_env.verify import exec_or_empty, process_running
from nika.telemetry import CONFIG_DIR, DEVICE_EXEC_WORKERS
from nika.utils.parallel import bounded_parallel_map

if TYPE_CHECKING:
    from nika.runtime.base import LabRuntime

IPFIX_PORT = 9995
FLOW_DIR = "/var/lib/flows"
FLOW_EXPORTERS_FILE = "/var/lib/flow_exporters"
# nfcapd rotation period; nfdump reads closed files, so this bounds query lag.
FLOW_ROTATE_SEC = 10
# pmacct nfprobe timeouts: export active flows every 10 s, idle flows after 10 s,
# and check for expired flows every 2 s.
NFPROBE_TIMEOUTS = (
    "general=10:maxlife=10:udp=10:icmp=10:tcp=10:tcp.fin=5:tcp.rst=5:expint=2"
)
PMACCT_DIR = "/etc/pmacct"
PLUGIN_PIPE_SIZE = 262144
NETFLOW_SCRIPT = f"{CONFIG_DIR}/netflow.sh"
# ``pmacctd -V`` prints e.g. "Promiscuous Mode Accounting Daemon, pmacctd 1.7.7-git".
_PMACCTD_VERSION = re.compile(r"\bpmacctd (\d[\w.\-]*)")
# ``ovs-vsctl --version`` prints e.g. "ovs-vsctl (Open vSwitch) 3.1.0".
_OVS_VERSION = re.compile(r"ovs-vsctl \(Open vSwitch\) (\d[\w.]*)")
OVS_VERSION_PREFIX = "ovs-"


def pmacct_conf_path(iface: str) -> str:
    return f"{PMACCT_DIR}/netflow-{iface}.conf"


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
            # The core-to-plugin buffer defaults to ~4 MB of shared memory per
            # probe, which the container's memory limit counts: a router with
            # 75 interfaces (isp_brain zib) would run out of memory.
            f"plugin_pipe_size: {PLUGIN_PIPE_SIZE}",
            "",
        ]
    )


def is_ovs(version: str) -> bool:
    """Whether ``version`` is an OVS NetFlow exporter rather than pmacct."""
    return version.startswith(OVS_VERSION_PREFIX)


def ovs_netflow_script(collector: str, address: str) -> str:
    """Configure NetFlow v5 on the datapath bridge toward ``collector``.

    UDP source is ``address`` because the management VRF policy route sends
    collector traffic out the management interface.
    """
    target = f"{collector}:{IPFIX_PORT}"
    return "\n".join(
        [
            "#!/bin/sh",
            "set -e",
            f"# mgmt exporter {address}",
            "br=$(ovs-vsctl --timeout=10 list-br | head -n1)",
            '[ -n "$br" ]',
            'ovs-vsctl --timeout=10 clear Bridge "$br" netflow 2>/dev/null || true',
            "ovs-vsctl --timeout=10 -- --id=@nf create NetFlow "
            f'targets=\\"{target}\\" '
            "active_timeout=10 "
            '-- set Bridge "$br" netflow=@nf',
            "",
        ]
    )


def probe(runtime: LabRuntime, name: str) -> str | None:
    """Return the pmacct version, or an ``ovs-`` version when only OVS is present."""
    out = exec_or_empty(
        runtime,
        name,
        "pmacctd -V 2>&1 | head -n 1; ovs-vsctl --version 2>&1 | head -n 1",
    )
    match = _PMACCTD_VERSION.search(out)
    if match:
        return match.group(1)
    match = _OVS_VERSION.search(out)
    if match:
        return f"{OVS_VERSION_PREFIX}{match.group(1)}"
    from nika.telemetry import routeros, srl

    return srl.probe(runtime, name) or routeros.probe(runtime, name)


def device_files(
    device: dict[str, Any],
    address: str,
    collector: str,
    version: str = "",
) -> dict[str, str]:
    from nika.telemetry import iosxr, routeros, srl

    if srl.is_srl(version) or iosxr.is_xr(version) or routeros.is_ros(version):
        return {}
    if is_ovs(version):
        return {NETFLOW_SCRIPT: ovs_netflow_script(collector, address)}
    return {
        pmacct_conf_path(iface): pmacct_conf(collector, iface, engine)
        for engine, iface in enumerate(device["data_ifaces"], 1)
    }


def device_lines(device: dict[str, Any], address: str, version: str = "") -> list[str]:
    from nika.telemetry import iosxr, routeros, srl

    if srl.is_srl(version) or iosxr.is_xr(version) or routeros.is_ros(version):
        return []
    if is_ovs(version):
        return [f"sh {NETFLOW_SCRIPT}"]
    # pmacctd cannot open a down interface, so wait for it; a running probe
    # pauses while its link is down and resumes when it comes back.
    return [
        f"(while true; do until grep -qx up /sys/class/net/{iface}/operstate "
        f"2>/dev/null; do sleep 1; done; pmacctd -f {pmacct_conf_path(iface)}; "
        "sleep 1; done) &"
        for iface in device["data_ifaces"]
    ]


def collector_files(
    targets: dict[str, str], versions: dict[str, str] | None = None
) -> dict[str, str]:
    """Exporter names by address, used by ``netflow_query`` to name exporters."""
    del versions
    return {
        FLOW_EXPORTERS_FILE: "".join(f"{ip} {name}\n" for name, ip in targets.items())
    }


def collector_lines(
    targets: dict[str, str] | None = None, versions: dict[str, str] | None = None
) -> list[str]:
    del targets, versions
    return [
        f"mkdir -p {FLOW_DIR}",
        f"nfcapd -D -w {FLOW_DIR} -t {FLOW_ROTATE_SEC} -p {IPFIX_PORT} "
        "-P /run/nfcapd.pid",
    ]


def _exporter_running(runtime: LabRuntime, name: str) -> bool:
    if process_running(runtime, name, "pmacctd"):
        return True
    if "targets" in exec_or_empty(
        runtime, name, "ovs-vsctl --timeout=3 list NetFlow 2>/dev/null"
    ):
        return True
    from nika.telemetry import routeros

    return routeros.traffic_flow_enabled(runtime, name)


def running(runtime: LabRuntime, collector: str, targets: dict[str, str]) -> bool:
    """The collector and every exporter probe are running."""
    return process_running(runtime, collector, "nfcapd") and all(
        bounded_parallel_map(
            lambda name: _exporter_running(runtime, name),
            sorted(targets),
            max_workers=DEVICE_EXEC_WORKERS,
        )
    )
