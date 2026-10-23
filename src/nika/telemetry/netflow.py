"""NetFlow/IPFIX telemetry: pmacct probes on capable devices, nfcapd collector.

A telemetry kind for ``nika.telemetry.management``, which wires the shared
management network and calls these functions.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from nika.net_env.verify import exec_or_empty, process_running
from nika.telemetry import DEVICE_EXEC_WORKERS
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
# ``pmacctd -V`` prints e.g. "Promiscuous Mode Accounting Daemon, pmacctd 1.7.7-git".
_PMACCTD_VERSION = re.compile(r"\bpmacctd (\d[\w.\-]*)")


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


def probe(runtime: LabRuntime, name: str) -> str | None:
    """Return the pmacct version installed in ``name``, if any."""
    out = exec_or_empty(runtime, name, "pmacctd -V 2>&1 | head -n 1")
    match = _PMACCTD_VERSION.search(out)
    return match.group(1) if match else None


def device_files(
    device: dict[str, Any], address: str, collector: str
) -> dict[str, str]:
    return {
        pmacct_conf_path(iface): pmacct_conf(collector, iface, engine)
        for engine, iface in enumerate(device["data_ifaces"], 1)
    }


def device_lines(device: dict[str, Any], address: str) -> list[str]:
    # pmacctd cannot open a down interface, so wait for it; a running probe
    # pauses while its link is down and resumes when it comes back.
    return [
        f"(while true; do until grep -qx up /sys/class/net/{iface}/operstate "
        f"2>/dev/null; do sleep 1; done; pmacctd -f {pmacct_conf_path(iface)}; "
        "sleep 1; done) &"
        for iface in device["data_ifaces"]
    ]


def collector_files(targets: dict[str, str]) -> dict[str, str]:
    """Exporter names by address, used by ``netflow_query`` to name exporters."""
    return {
        FLOW_EXPORTERS_FILE: "".join(f"{ip} {name}\n" for name, ip in targets.items())
    }


def collector_lines() -> list[str]:
    return [
        f"mkdir -p {FLOW_DIR}",
        f"nfcapd -D -w {FLOW_DIR} -t {FLOW_ROTATE_SEC} -p {IPFIX_PORT} "
        "-P /run/nfcapd.pid",
    ]


def running(runtime: LabRuntime, collector: str, targets: dict[str, str]) -> bool:
    """The collector and every exporter probe are running."""
    return process_running(runtime, collector, "nfcapd") and all(
        bounded_parallel_map(
            lambda name: process_running(runtime, name, "pmacctd"),
            sorted(targets),
            max_workers=DEVICE_EXEC_WORKERS,
        )
    )
