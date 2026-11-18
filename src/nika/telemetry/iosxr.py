"""IOS XR (XRd) Prometheus exporters for Kathara telemetry.

XRd keeps extra Kathara management NICs on Linux when they are not listed in
``XR_INTERFACES``. Interface counters and BGP session state come from
``xr_cli``. XRd Control Plane has no IPFIX or sFlow path that can reach the
collector without using a scenario interface.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from nika.net_env.verify import exec_or_empty
from nika.telemetry import CONFIG_DIR

if TYPE_CHECKING:
    from nika.runtime.base import LabRuntime

XR_VERSION_PREFIX = "xr-"
XR_EXPORTER = f"{CONFIG_DIR}/xr_exporter.py"
_VERSION = re.compile(r"\bVersion\s+(\d[\w.]*)", re.I)
_HEADER = re.compile(r"^(\S+) is ([^,]+), line protocol is (\S+)", re.I | re.M)
_INPUT = re.compile(r"(\d+) packets input, (\d+) bytes(?:, (\d+) total input drops)?")
_OUTPUT = re.compile(
    r"(\d+) packets output, (\d+) bytes(?:, (\d+) total output drops)?"
)
_IN_ERR = re.compile(r"(\d+) input errors")
_OUT_ERR = re.compile(r"(\d+) output errors")
_PEER = re.compile(r"^(\d+\.\d+\.\d+\.\d+)\s+")


def is_xr(version: str) -> bool:
    return version.startswith(XR_VERSION_PREFIX)


def probe(runtime: LabRuntime, name: str) -> str | None:
    """Return an ``xr-`` version when the node has ``xr_cli`` and Python 3."""
    out = exec_or_empty(
        runtime,
        name,
        "test -x /pkg/bin/xr_cli && echo xr_cli; command -v python3",
    )
    if "xr_cli" not in out.split() or "python3" not in out:
        return None
    shown = exec_or_empty(
        runtime, name, "/pkg/bin/xr_cli 'show version' 2>/dev/null | head -n 40"
    )
    match = _VERSION.search(shown)
    return (
        f"{XR_VERSION_PREFIX}{match.group(1)}"
        if match
        else f"{XR_VERSION_PREFIX}unknown"
    )


def parse_interfaces(output: str) -> list[dict[str, str | int]]:
    """Parse ``show interfaces`` counters, skipping management and loopbacks."""
    starts = list(_HEADER.finditer(output))
    rows: list[dict[str, str | int]] = []
    for index, match in enumerate(starts):
        name = match.group(1)
        if _skip_interface(name):
            continue
        end = starts[index + 1].start() if index + 1 < len(starts) else len(output)
        block = output[match.start() : end]
        inn = _INPUT.search(block)
        out = _OUTPUT.search(block)
        in_err = _IN_ERR.search(block)
        out_err = _OUT_ERR.search(block)
        if not inn or not out:
            continue
        line = match.group(3).lower()
        rows.append(
            {
                "device": name,
                "up": 1 if line == "up" else 0,
                "rx_packets": int(inn.group(1)),
                "rx_bytes": int(inn.group(2)),
                "rx_drops": int(inn.group(3) or 0),
                "rx_errors": int(in_err.group(1) if in_err else 0),
                "tx_packets": int(out.group(1)),
                "tx_bytes": int(out.group(2)),
                "tx_drops": int(out.group(3) or 0),
                "tx_errors": int(out_err.group(1) if out_err else 0),
            }
        )
    return rows


def parse_bgp_summary(output: str) -> list[dict[str, str | int]]:
    """Parse ``show bgp summary`` neighbor rows."""
    rows: list[dict[str, str | int]] = []
    for line in output.splitlines():
        match = _PEER.match(line)
        if not match:
            continue
        fields = line.split()
        if len(fields) < 10:
            continue
        state = fields[-1]
        established = state.isdigit() or state.lower() == "established"
        prefixes = int(state) if state.isdigit() else 0
        rows.append(
            {
                "neighbor": fields[0],
                "up": 1 if established else 0,
                "prefixes": prefixes,
            }
        )
    return rows


def _skip_interface(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith(("mgmt", "loopback", "null", "nvi"))


def exporter_script() -> str:
    """Prometheus exporter: interfaces and BGP summary on the mgmt VRF."""
    return r"""#!/usr/bin/env python3
import re
import socket
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

BIND, DEVICE = sys.argv[1], sys.argv[2]
PORT = 9100
HEADER = re.compile(
    r"^(\S+) is ([^,]+), line protocol is (\S+)", re.I | re.M
)
INPUT = re.compile(
    r"(\d+) packets input, (\d+) bytes(?:, (\d+) total input drops)?"
)
OUTPUT = re.compile(
    r"(\d+) packets output, (\d+) bytes(?:, (\d+) total output drops)?"
)
IN_ERR = re.compile(r"(\d+) input errors")
OUT_ERR = re.compile(r"(\d+) output errors")
PEER = re.compile(r"^(\d+\.\d+\.\d+\.\d+)\s+")


def _skip(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith(("mgmt", "loopback", "null", "nvi"))


def _cli(command: str) -> str:
    return subprocess.check_output(
        ["/pkg/bin/xr_cli", command],
        text=True,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )


def _interfaces(output: str):
    starts = list(HEADER.finditer(output))
    rows = []
    for index, match in enumerate(starts):
        name = match.group(1)
        if _skip(name):
            continue
        end = starts[index + 1].start() if index + 1 < len(starts) else len(output)
        block = output[match.start():end]
        inn = INPUT.search(block)
        out = OUTPUT.search(block)
        in_err = IN_ERR.search(block)
        out_err = OUT_ERR.search(block)
        if not inn or not out:
            continue
        line = match.group(3).lower()
        rows.append((
            name, 1 if line == "up" else 0,
            inn.group(1), inn.group(2), inn.group(3) or "0",
            in_err.group(1) if in_err else "0",
            out.group(1), out.group(2), out.group(3) or "0",
            out_err.group(1) if out_err else "0",
        ))
    return rows


def _bgp(output: str):
    rows = []
    for line in output.splitlines():
        if not PEER.match(line):
            continue
        fields = line.split()
        if len(fields) < 10:
            continue
        state = fields[-1]
        established = state.isdigit() or state.lower() == "established"
        prefixes = state if state.isdigit() else "0"
        rows.append((fields[0], 1 if established else 0, prefixes))
    return rows


def metrics() -> str:
    lines = [
        "# HELP xr_interface_up IOS XR line protocol is up.",
        "# TYPE xr_interface_up gauge",
        "# HELP xr_interface_in_packets IOS XR interface receive packets.",
        "# TYPE xr_interface_in_packets counter",
        "# HELP xr_interface_in_octets IOS XR interface receive octets.",
        "# TYPE xr_interface_in_octets counter",
        "# HELP xr_interface_in_drops IOS XR interface receive drops.",
        "# TYPE xr_interface_in_drops counter",
        "# HELP xr_interface_in_errors IOS XR interface receive errors.",
        "# TYPE xr_interface_in_errors counter",
        "# HELP xr_interface_out_packets IOS XR interface transmit packets.",
        "# TYPE xr_interface_out_packets counter",
        "# HELP xr_interface_out_octets IOS XR interface transmit octets.",
        "# TYPE xr_interface_out_octets counter",
        "# HELP xr_interface_out_drops IOS XR interface transmit drops.",
        "# TYPE xr_interface_out_drops counter",
        "# HELP xr_interface_out_errors IOS XR interface transmit errors.",
        "# TYPE xr_interface_out_errors counter",
        "# HELP xr_bgp_peer_up IOS XR BGP neighbor is Established.",
        "# TYPE xr_bgp_peer_up gauge",
        "# HELP xr_bgp_peer_prefixes IOS XR BGP prefixes received.",
        "# TYPE xr_bgp_peer_prefixes gauge",
    ]
    try:
        ifaces = _interfaces(_cli("show interfaces"))
    except (OSError, subprocess.CalledProcessError):
        ifaces = []
    for row in ifaces:
        labels = 'device="%s"' % row[0]
        lines.append("xr_interface_up{%s} %s" % (labels, row[1]))
        lines.append("xr_interface_in_packets{%s} %s" % (labels, row[2]))
        lines.append("xr_interface_in_octets{%s} %s" % (labels, row[3]))
        lines.append("xr_interface_in_drops{%s} %s" % (labels, row[4]))
        lines.append("xr_interface_in_errors{%s} %s" % (labels, row[5]))
        lines.append("xr_interface_out_packets{%s} %s" % (labels, row[6]))
        lines.append("xr_interface_out_octets{%s} %s" % (labels, row[7]))
        lines.append("xr_interface_out_drops{%s} %s" % (labels, row[8]))
        lines.append("xr_interface_out_errors{%s} %s" % (labels, row[9]))
    try:
        peers = _bgp(_cli("show bgp summary"))
    except (OSError, subprocess.CalledProcessError):
        peers = []
    for neighbor, up, prefixes in peers:
        labels = 'neighbor="%s"' % neighbor
        lines.append("xr_bgp_peer_up{%s} %s" % (labels, up))
        lines.append("xr_bgp_peer_prefixes{%s} %s" % (labels, prefixes))
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
