"""SONiC exporters for Containerlab telemetry.

SONiC-VS forwards in the Linux kernel on its ``Ethernet<N>`` ports and keeps
port counters in COUNTERS_DB, but the image ships no sFlow agent, gNMI server
or node_exporter. NIKA exports what a SONiC switch can:

* syslog: SONiC's rsyslogd forwards to the collector, with the server also
  recorded in CONFIG_DB ``SYSLOG_SERVER`` as ``config syslog add`` does;
* prometheus: a Python exporter in the router serves COUNTERS_DB port counters
  and FRR BGP sessions on the management address only;
* netflow: pmacctd IPFIX probes, the FRR exporter, run in a helper container
  that shares the router's network namespace and sees its ``Ethernet<N>`` ports.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from nika.net_env.utils.kathara.docker_files.docker_images import nika_image
from nika.net_env.utils.sonic.common import KIND, sonic_port
from nika.net_env.verify import exec_or_empty
from nika.telemetry import CONFIG_DIR

if TYPE_CHECKING:
    from nika.runtime.base import LabRuntime

SONIC_VERSION_PREFIX = "sonic-"
EXPORTER = f"{CONFIG_DIR}/sonic_exporter.py"
EXPORTER_PORT = 9100
# Container interface on the Containerlab management network.
MGMT_IFACE = "eth0"
# Sorted ahead of the image's 45-frr.conf, which stops FRR messages.
RSYSLOG_REMOTE = "/etc/rsyslog.d/00-remote.conf"
# Helper container running the flow probes in a router's network namespace.
FLOW_PROBE_PREFIX = "telemetry_flow_"
_ETH = re.compile(r"^eth([1-9][0-9]*)$")
_VERSION = re.compile(r"build_version:\s*'?([^'\s]+)")


def is_sonic(version: str) -> bool:
    return version.startswith(SONIC_VERSION_PREFIX)


def is_sonic_kind(kind: str) -> bool:
    return kind == KIND


def flow_probe_name(router: str) -> str:
    return f"{FLOW_PROBE_PREFIX}{router}"


def data_ports(name: str, links: list[dict[str, Any]]) -> list[str]:
    """SONiC ports of ``name``'s topology links, in port order."""
    prefix = f"{name}:"
    numbers = sorted(
        int(match.group(1))
        for link in links
        for endpoint in link.get("endpoints") or []
        if isinstance(endpoint, str) and endpoint.startswith(prefix)
        if (match := _ETH.match(endpoint.split(":", 1)[1]))
    )
    return [sonic_port(f"eth{number}") for number in numbers]


def flow_probe_node(router: str) -> dict[str, Any]:
    """Containerlab node sharing ``router``'s network namespace, for pmacctd."""
    return {
        "kind": "linux",
        "image": nika_image("frr"),
        "network-mode": f"container:{router}",
        "memory": "256M",
    }


def probe(runtime: LabRuntime, name: str) -> str | None:
    """Return a ``sonic-`` version when the router has SONiC's redis and FRR."""
    out = exec_or_empty(
        runtime,
        name,
        "for tool in python3 redis-cli vtysh; do command -v $tool; done; "
        "cat /etc/sonic/sonic_version.yml",
    )
    if not all(tool in out for tool in ("python3", "redis-cli", "vtysh")):
        return None
    match = _VERSION.search(out)
    return f"{SONIC_VERSION_PREFIX}{match.group(1) if match else 'unknown'}"


def syslog_files(collector: str) -> dict[str, str]:
    from nika.telemetry.syslog import SYSLOG_PORT

    return {
        RSYSLOG_REMOTE: (
            f'*.info action(type="omfwd" target="{collector}" '
            f'port="{SYSLOG_PORT}" protocol="udp")\n'
        )
    }


def syslog_lines(collector: str) -> list[str]:
    from nika.telemetry.syslog import SYSLOG_PORT

    return [
        f"sonic-db-cli CONFIG_DB hset 'SYSLOG_SERVER|{collector}' "
        f"port {SYSLOG_PORT} protocol udp severity info >/dev/null",
        "supervisorctl restart rsyslogd >/dev/null",
    ]


def syslog_enabled(runtime: LabRuntime, name: str) -> bool:
    out = exec_or_empty(
        runtime,
        name,
        f"test -s {RSYSLOG_REMOTE} && supervisorctl status rsyslogd",
    )
    return "RUNNING" in out


def exporter_lines(address: str) -> list[str]:
    return [
        f"(while true; do python3 {EXPORTER} {address} {MGMT_IFACE}; sleep 1; done) &"
    ]


def exporter_script() -> str:
    """Prometheus exporter: COUNTERS_DB port counters and FRR BGP sessions."""
    return r"""#!/usr/bin/env python3
import json
import socket
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

from swsscommon.swsscommon import SonicV2Connector

BIND, DEVICE = sys.argv[1], sys.argv[2]
PORT = 9100
# Metric suffix -> COUNTERS_DB port statistics summed into it, as in portstat.
COUNTERS = (
    ("in_octets", ("SAI_PORT_STAT_IF_IN_OCTETS",)),
    ("out_octets", ("SAI_PORT_STAT_IF_OUT_OCTETS",)),
    ("in_packets", ("SAI_PORT_STAT_IF_IN_UCAST_PKTS", "SAI_PORT_STAT_IF_IN_NON_UCAST_PKTS")),
    ("out_packets", ("SAI_PORT_STAT_IF_OUT_UCAST_PKTS", "SAI_PORT_STAT_IF_OUT_NON_UCAST_PKTS")),
    ("in_errors", ("SAI_PORT_STAT_IF_IN_ERRORS",)),
    ("out_errors", ("SAI_PORT_STAT_IF_OUT_ERRORS",)),
    ("in_discards", ("SAI_PORT_STAT_IF_IN_DISCARDS",)),
    ("out_discards", ("SAI_PORT_STAT_IF_OUT_DISCARDS",)),
)
HELP = (
    "# HELP sonic_interface_up SONiC port operational status is up.",
    "# TYPE sonic_interface_up gauge",
    *(
        line
        for name, _ in COUNTERS
        for line in (
            "# HELP sonic_interface_%s SONiC port %s." % (name, name.replace("_", " ")),
            "# TYPE sonic_interface_%s counter" % name,
        )
    ),
    "# HELP frr_bgp_peer_up FRR BGP neighbor is Established.",
    "# TYPE frr_bgp_peer_up gauge",
    "# HELP frr_bgp_peer_prefixes FRR BGP prefixes received.",
    "# TYPE frr_bgp_peer_prefixes gauge",
)


def _number(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def ports():
    db = SonicV2Connector(host="127.0.0.1")
    db.connect(db.COUNTERS_DB)
    db.connect(db.APPL_DB)
    rows = []
    names = db.get_all(db.COUNTERS_DB, "COUNTERS_PORT_NAME_MAP") or {}
    for port, oid in sorted(names.items()):
        stats = db.get_all(db.COUNTERS_DB, "COUNTERS:%s" % oid) or {}
        oper = db.get(db.APPL_DB, "PORT_TABLE:%s" % port, "oper_status")
        rows.append((port, 1 if oper == "up" else 0, stats))
    return rows


def peers():
    try:
        payload = json.loads(
            subprocess.check_output(
                ["vtysh", "-c", "show bgp summary json"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    rows = []
    seen = set()
    for family in payload.values() if isinstance(payload, dict) else ():
        if not isinstance(family, dict) or not isinstance(family.get("peers"), dict):
            continue
        for neighbor, row in family["peers"].items():
            if not isinstance(row, dict) or neighbor in seen:
                continue
            seen.add(neighbor)
            prefixes = row.get("pfxRcd")
            if prefixes is None:
                prefixes = row.get("prefixReceivedCount") or 0
            up = 1 if row.get("state") == "Established" else 0
            rows.append((neighbor, up, _number(prefixes)))
    return rows


def metrics():
    lines = list(HELP)
    for port, up, stats in ports():
        labels = 'device="%s"' % port
        lines.append("sonic_interface_up{%s} %s" % (labels, up))
        for name, fields in COUNTERS:
            value = sum(_number(stats.get(field)) for field in fields)
            lines.append("sonic_interface_%s{%s} %s" % (name, labels, value))
    for neighbor, up, prefixes in peers():
        labels = 'neighbor="%s"' % neighbor
        lines.append("frr_bgp_peer_up{%s} %s" % (labels, up))
        lines.append("frr_bgp_peer_prefixes{%s} %s" % (labels, prefixes))
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") not in ("", "/metrics"):
            self.send_error(404)
            return
        try:
            body = metrics().encode()
        except Exception:  # SONiC databases are restarting.
            self.send_error(503)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


class Server(HTTPServer):
    # Answer on the management interface only, not on routed ports.
    def server_bind(self):
        self.socket.setsockopt(
            socket.SOL_SOCKET, socket.SO_BINDTODEVICE, DEVICE.encode() + b"\0"
        )
        super().server_bind()


Server((BIND, PORT), Handler).serve_forever()
"""
