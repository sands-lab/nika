"""MikroTik RouterOS exporters for Kathara telemetry.

vrnetlab maps every container data NIC into the VM, including the extra
Kathara management NIC. Addresses and traffic-flow therefore live in
RouterOS, not in a Linux VRF. The collector scrapes interface counters over
SSH on that extra interface; NetFlow targets the collector on the same LAN.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any

from nika.net_env.verify import exec_or_empty
from nika.service.lab.routeros_api import CLI_COMMAND
from nika.telemetry import CONFIG_DIR

if TYPE_CHECKING:
    from nika.runtime.base import LabRuntime

ROS_VERSION_PREFIX = "ros-"
SETUP_SCRIPT = f"{CONFIG_DIR}/routeros_setup.sh"
SYSLOG_SCRIPT = f"{CONFIG_DIR}/routeros_syslog.sh"
EXPORTER = f"{CONFIG_DIR}/routeros_exporter.py"
EXPORTER_PORT = 9101
_VERSION = re.compile(r"\bversion:\s*(\d[\w.]*)", re.I)
_STATS_ROW = re.compile(
    r'(?:^|\s)(?:name="(?P<qname>[^"]+)"|(?P<name>ether\d+|lo0|bridge\d+))\s+'
    r"(?:rx-byte[=:]?\s*)?(?P<rx_byte>\d+)\s+"
    r"(?:tx-byte[=:]?\s*)?(?P<tx_byte>\d+)\s+"
    r"(?:rx-packet[=:]?\s*)?(?P<rx_packet>\d+)\s+"
    r"(?:tx-packet[=:]?\s*)?(?P<tx_packet>\d+)",
    re.I,
)
_COLUMN_ROW = re.compile(
    r"^(?P<name>ether\d+|lo0|bridge\d+)\s+"
    r"(?P<rx_byte>\d+)\s+(?P<tx_byte>\d+)\s+"
    r"(?P<rx_packet>\d+)\s+(?P<tx_packet>\d+)"
    r"(?:\s+(?P<rx_drop>\d+)\s+(?P<tx_drop>\d+)"
    r"\s+(?P<rx_error>\d+)\s+(?P<tx_error>\d+))?",
    re.I | re.M,
)
_TERSE = re.compile(
    r"^\s*\d+\s+(?P<flags>[A-Z]*)\s+(?P<name>ether\d+|lo0|bridge\d+)",
    re.I | re.M,
)
_BGP_ADDR = re.compile(r"(?:^|\s)remote\.address=(\S+)")
_BGP_UP = re.compile(r"(?:^|\s)established=(yes|true|no|false)\b", re.I)


def is_ros(version: str) -> bool:
    return version.startswith(ROS_VERSION_PREFIX)


def linux_to_ether(name: str) -> str:
    """Map Kathara ``ethN`` to RouterOS ``ether{N+1}`` (ether1 is vrnetlab mgmt)."""
    match = re.fullmatch(r"eth(\d+)", name)
    if not match:
        raise ValueError(f"Unsupported RouterOS Linux interface: {name!r}")
    return f"ether{int(match.group(1)) + 1}"


def probe(runtime: LabRuntime, name: str) -> str | None:
    """Return a ``ros-`` version when the node has the vrnetlab SSH hop."""
    path = exec_or_empty(runtime, name, "command -v sshpass 2>/dev/null").strip()
    if not path:
        return None
    shown = ""
    for _ in range(12):
        shown = exec_or_empty(
            runtime,
            name,
            CLI_COMMAND.format(command="/system resource print"),
            timeout=20,
        )
        if shown.strip():
            break
        time.sleep(2)
    if not shown.strip():
        return None
    match = _VERSION.search(shown)
    return (
        f"{ROS_VERSION_PREFIX}{match.group(1)}"
        if match
        else f"{ROS_VERSION_PREFIX}unknown"
    )


def traffic_flow_enabled(runtime: LabRuntime, name: str) -> bool:
    out = exec_or_empty(
        runtime,
        name,
        CLI_COMMAND.format(command="/ip traffic-flow print"),
        timeout=20,
    )
    lowered = out.lower()
    return "enabled" in lowered and ("yes" in lowered or "true" in lowered)


def parse_interface_stats(output: str) -> list[dict[str, str | int]]:
    """Parse ``/interface print stats`` rows, plus terse running flags."""
    running = {
        match.group("name"): 1 if "R" in match.group("flags").upper() else 0
        for match in _TERSE.finditer(output)
    }
    rows: list[dict[str, str | int]] = []
    seen: set[str] = set()
    for match in (*_COLUMN_ROW.finditer(output), *_STATS_ROW.finditer(output)):
        name = (match.group("name") or match.groupdict().get("qname") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        groups = match.groupdict()
        rows.append(
            {
                "device": name,
                "up": running.get(name, 0),
                "rx_bytes": int(match.group("rx_byte")),
                "tx_bytes": int(match.group("tx_byte")),
                "rx_packets": int(match.group("rx_packet")),
                "tx_packets": int(match.group("tx_packet")),
                "rx_drops": int(groups.get("rx_drop") or 0),
                "tx_drops": int(groups.get("tx_drop") or 0),
                "rx_errors": int(groups.get("rx_error") or 0),
                "tx_errors": int(groups.get("tx_error") or 0),
            }
        )
    return rows


def parse_bgp_sessions(output: str) -> list[dict[str, str | int]]:
    """Parse ``/routing bgp session print terse`` rows."""
    rows: list[dict[str, str | int]] = []
    for line in output.splitlines():
        address = _BGP_ADDR.search(line)
        if not address:
            continue
        established = _BGP_UP.search(line)
        up = 1 if established and established.group(1).lower() in {"yes", "true"} else 0
        rows.append({"neighbor": address.group(1), "up": up})
    return rows


def setup_script(device: dict[str, Any], address: str, collector: str = "") -> str:
    """Assign the extra ether address and optionally enable traffic-flow."""
    ether = linux_to_ether(str(device["mgmt_iface"]))
    prefixlen = int(device["prefixlen"])
    cli = CLI_COMMAND
    lines = [
        "#!/bin/sh",
        "set -e",
        _ssh(
            cli,
            f":if ([:len [/ip address find where interface={ether}]] = 0) do={{ "
            f"/ip address add address={address}/{prefixlen} interface={ether} }}",
        ),
    ]
    if collector:
        from nika.telemetry.netflow import IPFIX_PORT

        lines.append(
            _ssh(
                cli,
                "/ip traffic-flow set enabled=yes interfaces=all",
            )
        )
        lines.append(
            _ssh(
                cli,
                ":if ([:len [/ip traffic-flow target find]] = 0) do={{ "
                f"/ip traffic-flow target add dst-address={collector} "
                f"port={IPFIX_PORT} version=9 }}",
            )
        )
    return "\n".join(lines) + "\n"


def syslog_script(address: str, collector: str) -> str:
    """Point the default RouterOS remote action at the collector.

    RouterOS already has a ``remote`` action (``remote=0.0.0.0``). SSH
    quoting cannot carry RouterOS ``do={...}`` braces, so this only uses
    ``set`` / ``add`` lines.
    """
    from nika.telemetry.syslog import SYSLOG_PORT

    cli = CLI_COMMAND
    return (
        "\n".join(
            [
                "#!/bin/sh",
                "set -e",
                _ssh(
                    cli,
                    f"/system logging action set remote remote={collector} "
                    f"remote-port={SYSLOG_PORT} src-address={address}",
                ),
                _ssh(cli, "/system logging add action=remote topics=info") + " || true",
            ]
        )
        + "\n"
    )


def syslog_enabled(runtime: LabRuntime, name: str) -> bool:
    out = exec_or_empty(
        runtime,
        name,
        CLI_COMMAND.format(command="/system logging action print"),
        timeout=20,
    )
    return any(
        ip != "0.0.0.0" for ip in re.findall(r"remote=(\d+\.\d+\.\d+\.\d+)", out)
    )


def _ssh(cli: str, command: str) -> str:
    return cli.format(command=command)


def exporter_script() -> str:
    """Prometheus exporter on the collector: SSH to each RouterOS telemetry IP."""
    return r"""#!/usr/bin/env python3
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer

BIND = sys.argv[1]
PORT = int(sys.argv[2])
TARGETS = json.loads(sys.argv[3])
HIDDEN = set(sys.argv[4].split(",")) if len(sys.argv) > 4 and sys.argv[4] else set()
USER = os.environ.get("ROS_USER", "vrnetlab")
PASSWORD = os.environ.get("ROS_PASSWORD", "VR-netlab9")


def _ssh(address: str, command: str) -> str:
    return subprocess.check_output(
        [
            "sshpass", "-p", PASSWORD, "ssh", "-q",
            "-oStrictHostKeyChecking=no",
            "-oUserKnownHostsFile=/dev/null",
            "-oConnectTimeout=3",
            "-oHostKeyAlgorithms=+ssh-rsa",
            "-oPubkeyAuthentication=no",
            "%s@%s" % (USER, address),
            command,
        ],
        stderr=subprocess.DEVNULL,
        timeout=20,
        text=True,
    )


COLUMN = re.compile(
    r"^(ether\d+|lo0|bridge\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)"
    r"(?:\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+))?",
    re.I | re.M,
)
STATS = re.compile(
    r'(?:^|\s)(?:name="([^"]+)"|(ether\d+|lo0|bridge\d+))\s+'
    r"(?:rx-byte[=:]?\s*)?(\d+)\s+"
    r"(?:tx-byte[=:]?\s*)?(\d+)\s+"
    r"(?:rx-packet[=:]?\s*)?(\d+)\s+"
    r"(?:tx-packet[=:]?\s*)?(\d+)",
    re.I,
)
TERSE = re.compile(
    r"^\s*\d+\s+([A-Z]*)\s+(ether\d+|lo0|bridge\d+)",
    re.I | re.M,
)
BGP_ADDR = re.compile(r"(?:^|\s)remote\.address=(\S+)")
BGP_UP = re.compile(r"(?:^|\s)established=(yes|true|no|false)\b", re.I)


def _rows(output: str):
    running = {}
    for match in TERSE.finditer(output):
        running[match.group(2)] = 1 if "R" in match.group(1).upper() else 0
    seen = set()
    rows = []
    for match in COLUMN.finditer(output):
        name = match.group(1)
        if name in seen:
            continue
        seen.add(name)
        rows.append((
            name, running.get(name, 0),
            match.group(2), match.group(3), match.group(4), match.group(5),
            match.group(6) or "0", match.group(7) or "0",
            match.group(8) or "0", match.group(9) or "0",
        ))
    for match in STATS.finditer(output):
        name = (match.group(1) or match.group(2) or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        rows.append((
            name, running.get(name, 0),
            match.group(3), match.group(4), match.group(5), match.group(6),
            "0", "0", "0", "0",
        ))
    return rows


def _bgp(output: str):
    rows = []
    for line in output.splitlines():
        address = BGP_ADDR.search(line)
        if not address:
            continue
        flag = BGP_UP.search(line)
        up = 1 if flag and flag.group(1).lower() in ("yes", "true") else 0
        rows.append((address.group(1), up))
    return rows


def metrics():
    lines = [
        "# HELP routeros_interface_up RouterOS interface is running.",
        "# TYPE routeros_interface_up gauge",
        "# HELP routeros_interface_rx_bytes RouterOS interface receive bytes.",
        "# TYPE routeros_interface_rx_bytes counter",
        "# HELP routeros_interface_tx_bytes RouterOS interface transmit bytes.",
        "# TYPE routeros_interface_tx_bytes counter",
        "# HELP routeros_interface_rx_packets RouterOS interface receive packets.",
        "# TYPE routeros_interface_rx_packets counter",
        "# HELP routeros_interface_tx_packets RouterOS interface transmit packets.",
        "# TYPE routeros_interface_tx_packets counter",
        "# HELP routeros_interface_rx_drops RouterOS interface receive drops.",
        "# TYPE routeros_interface_rx_drops counter",
        "# HELP routeros_interface_tx_drops RouterOS interface transmit drops.",
        "# TYPE routeros_interface_tx_drops counter",
        "# HELP routeros_interface_rx_errors RouterOS interface receive errors.",
        "# TYPE routeros_interface_rx_errors counter",
        "# HELP routeros_interface_tx_errors RouterOS interface transmit errors.",
        "# TYPE routeros_interface_tx_errors counter",
        "# HELP routeros_bgp_peer_up RouterOS BGP session is established.",
        "# TYPE routeros_bgp_peer_up gauge",
    ]
    def _fetch(item):
        node, address = item
        try:
            stats = _ssh(
                address,
                "/interface print stats without-paging; "
                "/interface print terse without-paging",
            )
            try:
                bgp = _ssh(address, "/routing bgp session print terse without-paging")
            except (OSError, subprocess.CalledProcessError):
                bgp = ""
            return node, _rows(stats), _bgp(bgp)
        except (OSError, subprocess.CalledProcessError, ValueError, TypeError):
            return node, [], []

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(TARGETS)))) as pool:
        fetched = list(pool.map(_fetch, sorted(TARGETS.items())))
    for node, rows, peers in fetched:
        for name, up, rx_b, tx_b, rx_p, tx_p, rx_d, tx_d, rx_e, tx_e in rows:
            if name in HIDDEN or name == "ether1":
                continue
            labels = 'node="%s",device="%s"' % (node, name)
            lines.append("routeros_interface_up{%s} %s" % (labels, up))
            lines.append("routeros_interface_rx_bytes{%s} %s" % (labels, rx_b))
            lines.append("routeros_interface_tx_bytes{%s} %s" % (labels, tx_b))
            lines.append("routeros_interface_rx_packets{%s} %s" % (labels, rx_p))
            lines.append("routeros_interface_tx_packets{%s} %s" % (labels, tx_p))
            lines.append("routeros_interface_rx_drops{%s} %s" % (labels, rx_d))
            lines.append("routeros_interface_tx_drops{%s} %s" % (labels, tx_d))
            lines.append("routeros_interface_rx_errors{%s} %s" % (labels, rx_e))
            lines.append("routeros_interface_tx_errors{%s} %s" % (labels, tx_e))
        for neighbor, up in peers:
            labels = 'node="%s",neighbor="%s"' % (node, neighbor)
            lines.append("routeros_bgp_peer_up{%s} %s" % (labels, up))
    return ("\n".join(lines) + "\n").encode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/metrics", "/"):
            self.send_error(404)
            return
        body = metrics()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


HTTPServer((BIND, PORT), Handler).serve_forever()
"""
