"""SR Linux exporters for Containerlab telemetry.

gNMI on the lab management network scrapes interface counters. sFlow cannot
use that network, so flow records go to the collector over a separate
ip-vrf and extra ethernet port.
"""

from __future__ import annotations

import re
import subprocess
from typing import TYPE_CHECKING

from nika.net_env.verify import exec_or_empty

if TYPE_CHECKING:
    from nika.runtime.base import LabRuntime

SRL_VERSION_PREFIX = "srl-"
SRL_GNMI_USER = "admin"
SRL_GNMI_PASSWORD = "NokiaSrl1!"
SRL_GNMI_PORT = 57400
SFLOW_PORT = 6343
# Nokia SR Linux rejects sample-rate values below 256 on ixr-d2l.
SFLOW_SAMPLE_RATE = 256
TELEMETRY_NI = "telemetry"
_VERSION = re.compile(r"v?(\d+\.\d+[\w.\-]*)")


def is_srl(version: str) -> bool:
    return version.startswith(SRL_VERSION_PREFIX)


def yang_ethernet(e1: int) -> str:
    return f"ethernet-1/{e1}"


def yang_subif(e1: int) -> str:
    return f"{yang_ethernet(e1)}.0"


def clab_e1(e1: int) -> str:
    return f"e1-{e1}"


def probe(runtime: LabRuntime, name: str) -> str | None:
    """Return an ``srl-`` version when the node has ``sr_cli``."""
    path = exec_or_empty(runtime, name, "command -v sr_cli 2>/dev/null").strip()
    if "sr_cli" not in path:
        return None
    shown = exec_or_empty(
        runtime, name, 'sr_cli "show version" 2>/dev/null | head -n 20'
    )
    match = _VERSION.search(shown)
    return (
        f"{SRL_VERSION_PREFIX}{match.group(1)}"
        if match
        else f"{SRL_VERSION_PREFIX}unknown"
    )


def gnmic_argv(mgmt_ip: str, *args: str) -> list[str]:
    return [
        "gnmic",
        "-a",
        f"{mgmt_ip}:{SRL_GNMI_PORT}",
        "--timeout",
        "30s",
        "-u",
        SRL_GNMI_USER,
        "-p",
        SRL_GNMI_PASSWORD,
        "-e",
        "json_ietf",
        "--skip-verify",
        *args,
    ]


def gnmic_set(mgmt_ip: str, updates: list[tuple[str, str]]) -> None:
    """Merge YANG leaves on ``mgmt_ip`` with ``gnmic set``."""
    cmd = gnmic_argv(mgmt_ip, "set")
    for path, value in updates:
        cmd += ["--update-path", path, "--update-value", str(value)]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"gnmic set {mgmt_ip} failed: {detail[:800]}")


def enable_sflow(
    *,
    mgmt_ip: str,
    collector: str,
    source: str,
    telemetry_e1: int,
    data_e1: list[int],
    prefix: str,
) -> None:
    """Put the extra port in an ip-vrf and export sFlow to ``collector``."""
    srl_if = yang_ethernet(telemetry_e1)
    subif = yang_subif(telemetry_e1)
    gnmic_set(
        mgmt_ip,
        [
            (f"/interface[name={srl_if}]/admin-state", "enable"),
            (f"/interface[name={srl_if}]/subinterface[index=0]/admin-state", "enable"),
            (
                f"/interface[name={srl_if}]/subinterface[index=0]/ipv4/admin-state",
                "enable",
            ),
            (
                f"/interface[name={srl_if}]/subinterface[index=0]"
                f"/ipv4/address[ip-prefix={prefix}]",
                "{}",
            ),
            (f"/network-instance[name={TELEMETRY_NI}]/type", "ip-vrf"),
            (f"/network-instance[name={TELEMETRY_NI}]/admin-state", "enable"),
            (
                f"/network-instance[name={TELEMETRY_NI}]/interface[name={subif}]",
                "{}",
            ),
        ],
    )
    updates: list[tuple[str, str]] = [
        ("/system/sflow/admin-state", "enable"),
        ("/system/sflow/sample-rate", str(SFLOW_SAMPLE_RATE)),
        (
            "/system/sflow/collector[collector-id=1]/collector-address",
            collector,
        ),
        ("/system/sflow/collector[collector-id=1]/source-address", source),
        (
            "/system/sflow/collector[collector-id=1]/network-instance",
            TELEMETRY_NI,
        ),
        ("/system/sflow/collector[collector-id=1]/port", str(SFLOW_PORT)),
    ]
    for e1 in data_e1:
        updates.append(
            (f"/interface[name={yang_ethernet(e1)}]/sflow/admin-state", "enable")
        )
    gnmic_set(mgmt_ip, updates)


def enable_syslog(*, mgmt_ip: str, collector: str) -> None:
    """Send SR Linux logs to ``collector`` from the mgmt network-instance.

    SR Linux 24.10 places ``network-instance`` on ``/system/logging``, not on
    the remote-server. Without it, rsyslogd runs in the default instance and
    cannot reach the lab management network. Subsystem messages use Linux
    facility local6 by default.
    """
    prefix = f"/system/logging/remote-server[host={collector}]"
    updates = [
        ("/system/logging/network-instance", "mgmt"),
        (f"{prefix}/transport", "udp"),
        (f"{prefix}/remote-port", str(514)),
    ]
    for facility in ("local6", "local7", "daemon", "syslog", "kern"):
        updates.append(
            (
                f"{prefix}/facility[facility-name={facility}]/priority/match-above",
                "informational",
            )
        )
    gnmic_set(mgmt_ip, updates)


def syslog_enabled(runtime: LabRuntime, name: str) -> bool:
    out = exec_or_empty(
        runtime,
        name,
        'sr_cli "info from running system logging" 2>/dev/null',
    )
    lowered = out.lower()
    return "remote-server" in lowered or "remote-port" in lowered


def collect_interfaces(payload: object) -> tuple[dict[str, dict], dict[str, str]]:
    """Walk a gNMI ``/interface`` JSON payload into stats and oper-state."""
    stats: dict[str, dict] = {}
    oper: dict[str, str] = {}

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            name = obj.get("name")
            block = obj.get("statistics") or obj.get("srl_nokia-interfaces:statistics")
            state = obj.get("oper-state") or obj.get("srl_nokia-interfaces:oper-state")
            if isinstance(name, str):
                if isinstance(block, dict):
                    stats[name] = block
                if isinstance(state, str):
                    oper[name] = state
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(payload)
    return stats, oper


def collect_bgp_peers(payload: object) -> list[dict[str, str]]:
    """Walk a gNMI BGP neighbor payload into peer address and session state."""
    peers: list[dict[str, str]] = []

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            address = (
                obj.get("peer-address")
                or obj.get("srl_nokia-bgp:peer-address")
                or obj.get("neighbor-address")
            )
            state = (
                obj.get("session-state")
                or obj.get("srl_nokia-bgp:session-state")
                or obj.get("oper-state")
            )
            if isinstance(address, str) and isinstance(state, str):
                peers.append({"neighbor": address, "state": state.lower()})
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(payload)
    return peers


def sflow_enabled(runtime: LabRuntime, name: str) -> bool:
    out = exec_or_empty(
        runtime,
        name,
        'sr_cli "info from state system sflow admin-state" 2>/dev/null',
    )
    return "enable" in out.lower()


def exporter_script() -> str:
    """Prometheus textfile exporter: gNMI interface counters labelled ``node``."""
    return r"""#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer

BIND = sys.argv[1]
PORT = int(sys.argv[2])
TARGETS = json.loads(sys.argv[3])
HIDDEN = set(sys.argv[4].split(",")) if len(sys.argv) > 4 and sys.argv[4] else set()
USER = os.environ.get("SRL_GNMI_USER", "admin")
PASSWORD = os.environ.get("SRL_GNMI_PASSWORD", "NokiaSrl1!")


def _gnmic(mgmt_ip, *args):
    return subprocess.check_output(
        [
            "gnmic", "-a", "%s:57400" % mgmt_ip, "--timeout", "12s",
            "-u", USER, "-p", PASSWORD, "-e", "json_ietf", "--skip-verify", *args,
        ],
        stderr=subprocess.DEVNULL,
        timeout=20,
        text=True,
    )


def _walk_ifaces(obj, stats, oper):
    if isinstance(obj, dict):
        name = obj.get("name")
        block = obj.get("statistics") or obj.get("srl_nokia-interfaces:statistics")
        state = obj.get("oper-state") or obj.get("srl_nokia-interfaces:oper-state")
        if isinstance(name, str):
            if isinstance(block, dict):
                stats[name] = block
            if isinstance(state, str):
                oper[name] = state
        for value in obj.values():
            _walk_ifaces(value, stats, oper)
    elif isinstance(obj, list):
        for item in obj:
            _walk_ifaces(item, stats, oper)


def _walk_bgp(obj, peers):
    if isinstance(obj, dict):
        address = (
            obj.get("peer-address")
            or obj.get("srl_nokia-bgp:peer-address")
            or obj.get("neighbor-address")
        )
        state = (
            obj.get("session-state")
            or obj.get("srl_nokia-bgp:session-state")
        )
        if isinstance(address, str) and isinstance(state, str):
            peers.append((address, state.lower()))
        for value in obj.values():
            _walk_bgp(value, peers)
    elif isinstance(obj, list):
        for item in obj:
            _walk_bgp(item, peers)


def _number(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0


def metrics():
    lines = [
        "# HELP srl_interface_up SR Linux interface operational state is up.",
        "# TYPE srl_interface_up gauge",
        "# HELP srl_interface_in_octets SR Linux interface receive octets.",
        "# TYPE srl_interface_in_octets counter",
        "# HELP srl_interface_out_octets SR Linux interface transmit octets.",
        "# TYPE srl_interface_out_octets counter",
        "# HELP srl_interface_in_packets SR Linux interface receive packets.",
        "# TYPE srl_interface_in_packets counter",
        "# HELP srl_interface_out_packets SR Linux interface transmit packets.",
        "# TYPE srl_interface_out_packets counter",
        "# HELP srl_interface_in_errors SR Linux interface receive errors.",
        "# TYPE srl_interface_in_errors counter",
        "# HELP srl_interface_out_errors SR Linux interface transmit errors.",
        "# TYPE srl_interface_out_errors counter",
        "# HELP srl_interface_in_discards SR Linux interface receive discards.",
        "# TYPE srl_interface_in_discards counter",
        "# HELP srl_interface_out_discards SR Linux interface transmit discards.",
        "# TYPE srl_interface_out_discards counter",
        "# HELP srl_bgp_peer_up SR Linux BGP neighbor is established.",
        "# TYPE srl_bgp_peer_up gauge",
    ]
    def _fetch(item):
        node, mgmt_ip = item
        stats, oper, peers = {}, {}, []
        try:
            payload = json.loads(_gnmic(mgmt_ip, "get", "--path", "/interface"))
            _walk_ifaces(payload, stats, oper)
        except (OSError, subprocess.CalledProcessError, ValueError, TypeError):
            pass
        try:
            bgp = json.loads(
                _gnmic(mgmt_ip, "get", "--path", "/network-instance/protocols/bgp/neighbor")
            )
            _walk_bgp(bgp, peers)
        except (OSError, subprocess.CalledProcessError, ValueError, TypeError):
            pass
        return node, stats, oper, peers

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(TARGETS)))) as pool:
        fetched = list(pool.map(_fetch, sorted(TARGETS.items())))
    for node, stats, oper, peers in fetched:
        devices = set(stats) | set(oper)
        for device in sorted(devices):
            if (
                device.startswith("mgmt")
                or device == "lo0"
                or any(device == hidden or device.startswith(hidden + ".") for hidden in HIDDEN)
            ):
                continue
            labels = 'node="%s",device="%s"' % (node, device)
            block = stats.get(device) or {}
            inn = _number(block.get("in-octets"))
            out = _number(block.get("out-octets"))
            in_pkts = _number(
                block.get("in-unicast-packets")
            ) + _number(block.get("in-broadcast-packets")) + _number(
                block.get("in-multicast-packets")
            )
            out_pkts = _number(
                block.get("out-unicast-packets")
            ) + _number(block.get("out-broadcast-packets")) + _number(
                block.get("out-multicast-packets")
            )
            lines.append(
                "srl_interface_up{%s} %s"
                % (labels, 1 if (oper.get(device) or "").lower() == "up" else 0)
            )
            lines.append("srl_interface_in_octets{%s} %s" % (labels, inn))
            lines.append("srl_interface_out_octets{%s} %s" % (labels, out))
            lines.append("srl_interface_in_packets{%s} %s" % (labels, in_pkts))
            lines.append("srl_interface_out_packets{%s} %s" % (labels, out_pkts))
            lines.append(
                "srl_interface_in_errors{%s} %s"
                % (labels, _number(block.get("in-error-packets")))
            )
            lines.append(
                "srl_interface_out_errors{%s} %s"
                % (labels, _number(block.get("out-error-packets")))
            )
            lines.append(
                "srl_interface_in_discards{%s} %s"
                % (labels, _number(block.get("in-discarded-packets")))
            )
            lines.append(
                "srl_interface_out_discards{%s} %s"
                % (labels, _number(block.get("out-discarded-packets")))
            )
        for address, state in peers:
            labels = 'node="%s",neighbor="%s"' % (node, address)
            lines.append(
                "srl_bgp_peer_up{%s} %s" % (labels, 1 if state == "established" else 0)
            )
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
