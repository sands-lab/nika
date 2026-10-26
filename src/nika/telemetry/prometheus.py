"""Prometheus telemetry: node_exporter, OpenFlow, P4, IOS XR, or RouterOS.

A telemetry kind for ``nika.telemetry.management``, which wires the shared
management network and calls these functions.

node_exporter listens on the device's loopback only. A ``socat`` relay bound to
the management VRF exposes it on the management address, so the collector can
scrape it while the data plane cannot reach it. (A socket outside the VRF can
neither bind that address nor accept on it, and Kathara keeps ``/proc/sys``
read-only, so the ``tcp_l3mdev_accept`` sysctl is not an option.)

OVS switches have no node_exporter. They serve OpenFlow port counters from a
small Python exporter bound to the management address. P4 fabric managers that
already run node_exporter also write pipeline counters as a textfile that
node_exporter scrapes; PromQL keeps those series under the switch name.
XRd routers scrape ``xr_cli`` interface counters the same way. RouterOS
counters are scraped from the collector over SSH.
"""

from __future__ import annotations

import json
import re
import shlex
from typing import TYPE_CHECKING, Any

from nika.net_env.verify import exec_or_empty, process_running
from nika.telemetry import CONFIG_DIR, MGMT_VRF

if TYPE_CHECKING:
    from nika.runtime.base import LabRuntime

EXPORTER_PORT = 9100
PROMETHEUS_URL = "http://127.0.0.1:9090"
PROMETHEUS_CONFIG = f"{CONFIG_DIR}/prometheus.yml"
PROMETHEUS_DATA = "/var/lib/prometheus"
TEXTFILE_DIR = "/var/lib/node_exporter"
OVS_EXPORTER = f"{CONFIG_DIR}/openflow_exporter.py"
P4_EXPORTER = f"{CONFIG_DIR}/p4_exporter.py"
FRR_BGP_EXPORTER = f"{CONFIG_DIR}/frr_bgp_exporter.py"
SCRAPE_INTERVAL = "5s"
# Collectors that read the device's own network namespace. CPU, memory, and disk
# collectors would report the shared host kernel, the same on every device.
NODE_COLLECTORS = ("netdev", "netclass", "netstat", "sockstat", "conntrack", "arp")
# ``prometheus-node-exporter --version`` prints e.g. "node_exporter, version 1.5.0".
_NODE_EXPORTER_VERSION = re.compile(r"\bnode_exporter, version (\d[\w.+\-]*)")
_OVS_VERSION = re.compile(r"ovs-vsctl \(Open vSwitch\) (\d[\w.]*)")
_PORT_STATS = re.compile(
    r"port\s+(\d+|LOCAL):\s*"
    r"rx pkts=(\d+), bytes=(\d+), drop=(\d+), errs=(\d+),"
    r"[^\n]*\n\s*"
    r"tx pkts=(\d+), bytes=(\d+), drop=(\d+), errs=(\d+)",
    re.I,
)
_PORT_DESC = re.compile(r"^\s*(\d+|LOCAL)\(([^)]+)\):", re.I | re.M)
OVS_VERSION_PREFIX = "ovs-"
P4_TOKEN = "p4"
P4RT_MANAGER = "/opt/p4rt/p4rt_manager.py"
_P4_COUNTERS = (
    ("ingress", "p4_ingress"),
    ("egress", "p4_egress"),
    ("flow_syn", "p4_flow_syn"),
    ("flow_non_syn", "p4_flow_non_syn"),
)


def is_ovs(version: str) -> bool:
    return version.startswith(OVS_VERSION_PREFIX)


def has_p4(version: str) -> bool:
    return P4_TOKEN in version.split("+")


def parse_openflow_ports(dump_ports: str, dump_ports_desc: str = "") -> list[dict]:
    """Parse ``ovs-ofctl dump-ports`` plus optional ``dump-ports-desc``."""
    names = {
        match.group(1): match.group(2) for match in _PORT_DESC.finditer(dump_ports_desc)
    }
    rows = []
    for match in _PORT_STATS.finditer(dump_ports):
        port = match.group(1)
        rows.append(
            {
                "port": port,
                "device": names.get(port, names.get(port.lower(), port)),
                "rx_packets": int(match.group(2)),
                "rx_bytes": int(match.group(3)),
                "rx_dropped": int(match.group(4)),
                "rx_errors": int(match.group(5)),
                "tx_packets": int(match.group(6)),
                "tx_bytes": int(match.group(7)),
                "tx_dropped": int(match.group(8)),
                "tx_errors": int(match.group(9)),
            }
        )
    return rows


def render_openflow_metrics(rows: list[dict]) -> str:
    """Prometheus text for OpenFlow port counters."""
    lines = [
        "# HELP openflow_port_rx_packets OpenFlow receive packets.",
        "# TYPE openflow_port_rx_packets counter",
        "# HELP openflow_port_rx_bytes OpenFlow receive bytes.",
        "# TYPE openflow_port_rx_bytes counter",
        "# HELP openflow_port_rx_dropped OpenFlow receive drops.",
        "# TYPE openflow_port_rx_dropped counter",
        "# HELP openflow_port_rx_errors OpenFlow receive errors.",
        "# TYPE openflow_port_rx_errors counter",
        "# HELP openflow_port_tx_packets OpenFlow transmit packets.",
        "# TYPE openflow_port_tx_packets counter",
        "# HELP openflow_port_tx_bytes OpenFlow transmit bytes.",
        "# TYPE openflow_port_tx_bytes counter",
        "# HELP openflow_port_tx_dropped OpenFlow transmit drops.",
        "# TYPE openflow_port_tx_dropped counter",
        "# HELP openflow_port_tx_errors OpenFlow transmit errors.",
        "# TYPE openflow_port_tx_errors counter",
    ]
    for row in rows:
        labels = f'device="{row["device"]}",port="{row["port"]}"'
        for field in (
            "rx_packets",
            "rx_bytes",
            "rx_dropped",
            "rx_errors",
            "tx_packets",
            "tx_bytes",
            "tx_dropped",
            "tx_errors",
        ):
            lines.append(f"openflow_port_{field}{{{labels}}} {row[field]}")
    return "\n".join(lines) + "\n"


def render_p4_metrics(payload: dict[str, Any]) -> str:
    """Prometheus text for P4Runtime counter reads.

    ``switch`` is the BMv2 node. The scrape config copies it onto ``node`` so
    PromQL matches the other device series.
    """
    lines = []
    seen: set[str] = set()
    counters = payload.get("counters") or {}
    for bucket, metric in _P4_COUNTERS:
        for kind in ("packets", "bytes"):
            name = f"{metric}_{kind}"
            if name not in seen:
                lines.append(f"# HELP {name} P4 {bucket} {kind}.")
                lines.append(f"# TYPE {name} counter")
                seen.add(name)
            for switch, values in sorted(counters.items()):
                ports = (values or {}).get(bucket) or {}
                for port, counts in sorted(
                    ports.items(), key=lambda item: str(item[0])
                ):
                    if not isinstance(counts, dict):
                        continue
                    value = int(counts.get(kind) or 0)
                    lines.append(f'{name}{{switch="{switch}",port="{port}"}} {value}')
    return "\n".join(lines) + ("\n" if lines else "")


def _openflow_exporter_script() -> str:
    return r"""#!/usr/bin/env python3
import re
import socket
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

BIND, DEVICE = sys.argv[1], sys.argv[2]
PORT = 9100
STATS = re.compile(
    r"port\s+(\d+|LOCAL):\s*"
    r"rx pkts=(\d+), bytes=(\d+), drop=(\d+), errs=(\d+),"
    r"[^\n]*\n\s*"
    r"tx pkts=(\d+), bytes=(\d+), drop=(\d+), errs=(\d+)",
    re.I,
)
DESC = re.compile(r"^\s*(\d+|LOCAL)\(([^)]+)\):", re.I | re.M)


def _ofctl(*args: str) -> str:
    br = subprocess.check_output(
        ["ovs-vsctl", "--timeout=5", "list-br"], text=True
    ).splitlines()[0]
    return subprocess.check_output(
        ["ovs-ofctl", "-O", "OpenFlow13", "--timeout=5", *args, br],
        text=True,
        stderr=subprocess.DEVNULL,
    )


def metrics() -> str:
    names = {m.group(1): m.group(2) for m in DESC.finditer(_ofctl("dump-ports-desc"))}
    lines = []
    for m in STATS.finditer(_ofctl("dump-ports")):
        port = m.group(1)
        device = names.get(port, port)
        labels = 'device="%s",port="%s"' % (device, port)
        values = (
            ("rx_packets", m.group(2)),
            ("rx_bytes", m.group(3)),
            ("rx_dropped", m.group(4)),
            ("rx_errors", m.group(5)),
            ("tx_packets", m.group(6)),
            ("tx_bytes", m.group(7)),
            ("tx_dropped", m.group(8)),
            ("tx_errors", m.group(9)),
        )
        for field, value in values:
            lines.append("openflow_port_%s{%s} %s" % (field, labels, value))
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.rstrip("/") not in ("", "/metrics"):
            self.send_error(404)
            return
        body = metrics().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class Server(HTTPServer):
    def server_bind(self) -> None:
        self.socket.setsockopt(
            socket.SOL_SOCKET, socket.SO_BINDTODEVICE, DEVICE.encode() + b"\0"
        )
        super().server_bind()


Server((BIND, PORT), Handler).serve_forever()
"""


def _p4_exporter_script() -> str:
    return r"""#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile

INTENT = "/tmp/p4_fabric/intent.json"
P4INFO = "/tmp/p4_fabric/fabric.p4info.txt"
JSON_PATH = "/tmp/p4_fabric/fabric.json"
MANAGER = "/opt/p4rt/p4rt_manager.py"
OUT = "/var/lib/node_exporter/p4.prom"
BUCKETS = (
    ("ingress", "p4_ingress"),
    ("egress", "p4_egress"),
    ("flow_syn", "p4_flow_syn"),
    ("flow_non_syn", "p4_flow_non_syn"),
)


def main() -> int:
    raw = subprocess.check_output(
        [
            "python3",
            MANAGER,
            "--intent",
            INTENT,
            "--p4info",
            P4INFO,
            "--json",
            JSON_PATH,
            "counters",
        ],
        stderr=subprocess.DEVNULL,
        timeout=120,
    )
    payload = json.loads(raw)
    lines = []
    counters = payload.get("counters") or {}
    for bucket, metric in BUCKETS:
        for kind in ("packets", "bytes"):
            name = "%s_%s" % (metric, kind)
            lines.append("# HELP %s P4 %s %s." % (name, bucket, kind))
            lines.append("# TYPE %s counter" % name)
            for switch, values in sorted(counters.items()):
                ports = (values or {}).get(bucket) or {}
                for port, counts in sorted(ports.items(), key=lambda item: str(item[0])):
                    if not isinstance(counts, dict):
                        continue
                    lines.append(
                        '%s{switch="%s",port="%s"} %s'
                        % (name, switch, port, int(counts.get(kind) or 0))
                    )
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(OUT), prefix="p4.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        os.replace(tmp, OUT)
    except Exception:
        os.unlink(tmp)
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""


def parse_frr_bgp_summary(payload: object) -> list[dict[str, str | int]]:
    """Parse FRR ``show bgp summary json`` into peer gauges."""
    if not isinstance(payload, dict):
        return []
    rows: list[dict[str, str | int]] = []
    seen: set[str] = set()
    for family in payload.values():
        if not isinstance(family, dict) or not isinstance(family.get("peers"), dict):
            continue
        for neighbor, row in family["peers"].items():
            if not isinstance(row, dict) or neighbor in seen:
                continue
            seen.add(neighbor)
            state = str(row.get("state") or "")
            prefixes = row.get("pfxRcd")
            if prefixes is None:
                prefixes = row.get("prefixReceivedCount") or 0
            rows.append(
                {
                    "neighbor": str(neighbor),
                    "up": 1 if state == "Established" else 0,
                    "prefixes": int(prefixes or 0),
                }
            )
    return rows


def _frr_bgp_exporter_script() -> str:
    return r"""#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile

OUT = "/var/lib/node_exporter/frr_bgp.prom"


def peers():
    try:
        raw = subprocess.check_output(
            ["vtysh", "-c", "show bgp summary json"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        payload = json.loads(raw)
    except (OSError, subprocess.CalledProcessError, ValueError, TypeError):
        return []
    if not isinstance(payload, dict):
        return []
    rows = []
    seen = set()
    for family in payload.values():
        if not isinstance(family, dict) or not isinstance(family.get("peers"), dict):
            continue
        for neighbor, row in family["peers"].items():
            if not isinstance(row, dict) or neighbor in seen:
                continue
            seen.add(neighbor)
            state = str(row.get("state") or "")
            prefixes = row.get("pfxRcd")
            if prefixes is None:
                prefixes = row.get("prefixReceivedCount") or 0
            rows.append((neighbor, 1 if state == "Established" else 0, int(prefixes or 0)))
    return rows


def main() -> int:
    lines = [
        "# HELP frr_bgp_peer_up FRR BGP neighbor is Established.",
        "# TYPE frr_bgp_peer_up gauge",
        "# HELP frr_bgp_peer_prefixes FRR BGP prefixes received.",
        "# TYPE frr_bgp_peer_prefixes gauge",
    ]
    for neighbor, up, prefixes in peers():
        labels = 'neighbor="%s"' % neighbor
        lines.append("frr_bgp_peer_up{%s} %s" % (labels, up))
        lines.append("frr_bgp_peer_prefixes{%s} %s" % (labels, prefixes))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(OUT), prefix="frr.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        os.replace(tmp, OUT)
    except Exception:
        os.unlink(tmp)
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""


def probe(runtime: LabRuntime, name: str) -> str | None:
    """Return a version string for node_exporter, OVS, and optional P4 counters."""
    out = exec_or_empty(
        runtime,
        name,
        "prometheus-node-exporter --version 2>&1 | head -n 1; "
        "command -v socat; "
        f"test -f {P4RT_MANAGER} && echo p4rt; "
        "ovs-vsctl --version 2>&1 | head -n 1; "
        "command -v python3; command -v ovs-ofctl",
    )
    node = _NODE_EXPORTER_VERSION.search(out)
    if node and "socat" in out:
        version = node.group(1)
        return f"{version}+{P4_TOKEN}" if "p4rt" in out.split() else version
    ovs = _OVS_VERSION.search(out)
    if ovs and "python3" in out and "ovs-ofctl" in out:
        return f"{OVS_VERSION_PREFIX}{ovs.group(1)}"
    from nika.telemetry import iosxr, routeros, srl

    return (
        srl.probe(runtime, name)
        or iosxr.probe(runtime, name)
        or routeros.probe(runtime, name)
    )


def device_files(
    device: dict[str, Any],
    address: str,
    collector: str,
    version: str = "",
) -> dict[str, str]:
    from nika.telemetry import iosxr, routeros, srl

    if srl.is_srl(version) or routeros.is_ros(version):
        return {}
    files: dict[str, str] = {}
    if iosxr.is_xr(version):
        files[iosxr.XR_EXPORTER] = iosxr.exporter_script()
        return files
    if is_ovs(version):
        files[OVS_EXPORTER] = _openflow_exporter_script()
        return files
    if has_p4(version):
        files[P4_EXPORTER] = _p4_exporter_script()
    files[FRR_BGP_EXPORTER] = _frr_bgp_exporter_script()
    return files


def device_lines(device: dict[str, Any], address: str, version: str = "") -> list[str]:
    from nika.telemetry import iosxr, routeros, srl

    if srl.is_srl(version) or routeros.is_ros(version):
        return []
    if iosxr.is_xr(version):
        return [
            f"(while true; do python3 {iosxr.XR_EXPORTER} {address} {MGMT_VRF}; "
            "sleep 1; done) &"
        ]
    if is_ovs(version):
        return [
            f"(while true; do python3 {OVS_EXPORTER} {address} {MGMT_VRF}; "
            "sleep 1; done) &"
        ]
    # The management interface stays out of the metrics, as it stays out of the
    # topology.
    hidden = f"'^({device['mgmt_iface']}|{MGMT_VRF}|lo)$'"
    exporter = " ".join(
        [
            "prometheus-node-exporter",
            f"--web.listen-address=127.0.0.1:{EXPORTER_PORT}",
            "--collector.disable-defaults",
            *(f"--collector.{name}" for name in NODE_COLLECTORS),
            "--collector.textfile",
            f"--collector.textfile.directory={TEXTFILE_DIR}",
            f"--collector.netdev.device-exclude={hidden}",
            f"--collector.netclass.ignored-devices={hidden}",
        ]
    )
    relay = (
        f"socat TCP-LISTEN:{EXPORTER_PORT},bind={address},"
        f"so-bindtodevice={MGMT_VRF},reuseaddr,fork "
        f"TCP:127.0.0.1:{EXPORTER_PORT}"
    )
    lines = [
        f"mkdir -p {TEXTFILE_DIR}",
        f"(while true; do {exporter}; sleep 1; done) &",
        f"(while true; do {relay}; sleep 1; done) &",
    ]
    if has_p4(version):
        lines.append(f"(while true; do python3 {P4_EXPORTER}; sleep 5; done) &")
    lines.append(f"(while true; do python3 {FRR_BGP_EXPORTER}; sleep 5; done) &")
    return lines


def collector_files(
    targets: dict[str, str], versions: dict[str, str] | None = None
) -> dict[str, str]:
    """Prometheus config scraping each device, labelled ``node`` with its name.

    Not ``device``: node_exporter already uses that label for interface names.
    P4 series carry ``switch``; relabel copies it onto ``node`` so queries use
    the BMv2 name. RouterOS series are scraped from a collector-side exporter.
    """
    from nika.telemetry import routeros

    versions = versions or {}
    ros_targets = {
        name: ip
        for name, ip in targets.items()
        if routeros.is_ros(versions.get(name, ""))
    }
    other = {name: ip for name, ip in targets.items() if name not in ros_targets}
    jobs: list[dict[str, Any]] = []
    if other:
        jobs.append(
            {
                "job_name": "node",
                "static_configs": [
                    {"targets": [f"{ip}:{EXPORTER_PORT}"], "labels": {"node": name}}
                    for name, ip in sorted(other.items())
                ],
                "metric_relabel_configs": [
                    {
                        "source_labels": ["__name__", "switch"],
                        "regex": "p4_.+;(.+)",
                        "target_label": "node",
                        "replacement": "$1",
                    }
                ],
            }
        )
    if ros_targets:
        jobs.append(
            {
                "job_name": "routeros",
                "scrape_interval": "15s",
                "scrape_timeout": "14s",
                "static_configs": [
                    {"targets": [f"127.0.0.1:{routeros.EXPORTER_PORT}"]}
                ],
            }
        )
    config = {
        "global": {"scrape_interval": SCRAPE_INTERVAL},
        "scrape_configs": jobs,
    }
    files = {PROMETHEUS_CONFIG: json.dumps(config, indent=1) + "\n"}
    if ros_targets:
        files[routeros.EXPORTER] = routeros.exporter_script()
    return files


def collector_lines(
    targets: dict[str, str] | None = None,
    versions: dict[str, str] | None = None,
    hidden: str = "",
) -> list[str]:
    from nika.telemetry import routeros

    versions = versions or {}
    targets = targets or {}
    ros_targets = {
        name: ip
        for name, ip in targets.items()
        if routeros.is_ros(versions.get(name, ""))
    }
    lines = [
        f"mkdir -p {PROMETHEUS_DATA}",
        "(while true; do prometheus "
        f"--config.file={PROMETHEUS_CONFIG} "
        f"--storage.tsdb.path={PROMETHEUS_DATA} "
        f"--web.listen-address={PROMETHEUS_URL.removeprefix('http://')}; "
        "sleep 1; done) &",
    ]
    if ros_targets:
        extra = f" {shlex.quote(hidden)}" if hidden else ""
        lines.append(
            "(while true; do "
            f"python3 {routeros.EXPORTER} 127.0.0.1 {routeros.EXPORTER_PORT} "
            f"{shlex.quote(json.dumps(ros_targets, separators=(',', ':')))}"
            f"{extra}; sleep 1; done) &"
        )
    return lines


_NODE_METRICS = (
    "srl_interface_in_packets",
    "xr_interface_in_packets",
    "routeros_interface_rx_packets",
    "frr_bgp_peer_up",
)


def _query_nodes(
    runtime: LabRuntime, collector: str, query: str, *, up_only: bool = False
) -> set[str]:
    out = exec_or_empty(
        runtime, collector, f"curl -s {PROMETHEUS_URL}/api/v1/query?query={query}"
    )
    try:
        results = json.loads(out)["data"]["result"]
    except (ValueError, KeyError, TypeError):
        return set()
    nodes: set[str] = set()
    for row in results:
        if up_only and str((row.get("value") or [None, ""])[1]) != "1":
            continue
        node = (row.get("metric") or {}).get("node")
        if node:
            nodes.add(node)
    return nodes


def running(runtime: LabRuntime, collector: str, targets: dict[str, str]) -> bool:
    """Prometheus runs and reports every device's exporter as up."""
    if not process_running(runtime, collector, "prometheus"):
        return False
    if not targets:
        return True
    missing = set(targets) - _query_nodes(runtime, collector, "up", up_only=True)
    if not missing:
        return True
    for metric in _NODE_METRICS:
        missing -= _query_nodes(runtime, collector, metric)
        if not missing:
            return True
    return False
