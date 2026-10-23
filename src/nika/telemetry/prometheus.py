"""Prometheus telemetry: node_exporter on capable devices, Prometheus on the collector.

A telemetry kind for ``nika.telemetry.management``, which wires the shared
management network and calls these functions.

node_exporter listens on the device's loopback only. A ``socat`` relay bound to
the management VRF exposes it on the management address, so the collector can
scrape it while the data plane cannot reach it. (A socket outside the VRF can
neither bind that address nor accept on it, and Kathara keeps ``/proc/sys``
read-only, so the ``tcp_l3mdev_accept`` sysctl is not an option.)
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from nika.net_env.verify import exec_or_empty, process_running
from nika.telemetry import TELEMETRY_NETWORK

if TYPE_CHECKING:
    from nika.runtime.base import LabRuntime

EXPORTER_PORT = 9100
PROMETHEUS_URL = "http://127.0.0.1:9090"
PROMETHEUS_CONFIG = "/etc/nika-telemetry/prometheus.yml"
PROMETHEUS_DATA = "/var/lib/nika/prometheus"
SCRAPE_INTERVAL = "5s"
# Collectors that read the device's own network namespace. CPU, memory, and disk
# collectors would report the shared host kernel, the same on every device.
NODE_COLLECTORS = ("netdev", "netclass", "netstat", "sockstat", "conntrack", "arp")
# ``prometheus-node-exporter --version`` prints e.g. "node_exporter, version 1.5.0".
_NODE_EXPORTER_VERSION = re.compile(r"\bnode_exporter, version (\d[\w.+\-]*)")


def probe(runtime: LabRuntime, name: str) -> str | None:
    """Return the node_exporter version in ``name`` when it also has ``socat``."""
    out = exec_or_empty(
        runtime,
        name,
        "prometheus-node-exporter --version 2>&1 | head -n 1; command -v socat",
    )
    match = _NODE_EXPORTER_VERSION.search(out)
    return match.group(1) if match and "socat" in out else None


def device_files(device: dict[str, Any], address: str, collector: str) -> dict:
    return {}


def device_lines(device: dict[str, Any], address: str) -> list[str]:
    # The management interface stays out of the metrics, like out of the topology.
    hidden = f"'^({device['export_iface']}|{TELEMETRY_NETWORK}|lo)$'"
    exporter = " ".join(
        [
            "prometheus-node-exporter",
            f"--web.listen-address=127.0.0.1:{EXPORTER_PORT}",
            "--collector.disable-defaults",
            *(f"--collector.{name}" for name in NODE_COLLECTORS),
            f"--collector.netdev.device-exclude={hidden}",
            f"--collector.netclass.ignored-devices={hidden}",
        ]
    )
    relay = (
        f"socat TCP-LISTEN:{EXPORTER_PORT},bind={address},"
        f"so-bindtodevice={TELEMETRY_NETWORK},reuseaddr,fork "
        f"TCP:127.0.0.1:{EXPORTER_PORT}"
    )
    return [
        f"(while true; do {exporter}; sleep 1; done) &",
        f"(while true; do {relay}; sleep 1; done) &",
    ]


def collector_files(targets: dict[str, str]) -> dict[str, str]:
    """Prometheus config scraping each device, labelled ``node`` with its name.

    Not ``device``: node_exporter already uses that label for interface names.
    """
    config = {
        "global": {"scrape_interval": SCRAPE_INTERVAL},
        "scrape_configs": [
            {
                "job_name": "node",
                "static_configs": [
                    {"targets": [f"{ip}:{EXPORTER_PORT}"], "labels": {"node": name}}
                    for name, ip in sorted(targets.items())
                ],
            }
        ],
    }
    # JSON is valid YAML, so no YAML writer is needed on either side.
    return {PROMETHEUS_CONFIG: json.dumps(config, indent=1) + "\n"}


def collector_lines() -> list[str]:
    return [
        f"mkdir -p {PROMETHEUS_DATA}",
        "(while true; do prometheus "
        f"--config.file={PROMETHEUS_CONFIG} "
        f"--storage.tsdb.path={PROMETHEUS_DATA} "
        f"--web.listen-address={PROMETHEUS_URL.removeprefix('http://')}; "
        "sleep 1; done) &",
    ]


def running(runtime: LabRuntime, collector: str, targets: dict[str, str]) -> bool:
    """Prometheus runs and reports every device's exporter as up."""
    if not process_running(runtime, collector, "prometheus"):
        return False
    if not targets:
        return True
    out = exec_or_empty(
        runtime, collector, f"curl -s {PROMETHEUS_URL}/api/v1/query?query=up"
    )
    try:
        results = json.loads(out)["data"]["result"]
    except (ValueError, KeyError, TypeError):
        return False
    up = {r["metric"].get("node") for r in results if r["value"][1] == "1"}
    return set(targets) <= up
