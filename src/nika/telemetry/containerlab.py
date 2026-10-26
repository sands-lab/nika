"""Attach and configure telemetry on a Containerlab topology.

The collector joins the existing lab management network so gNMI scrapes work.
sFlow cannot use that network, so when ``netflow`` is on, each SR Linux node
gets one extra ethernet in an ip-vrf toward the collector. Those links are not
scenario topology.
"""

from __future__ import annotations

import ipaddress
import json
import posixpath
import re
import shlex
import shutil
from pathlib import Path
from typing import Any

import yaml

from nika.net_env.utils.kathara.docker_files.docker_images import nika_image
from nika.runtime.spec import NodeRole
from nika.telemetry import (
    CONFIG_DIR,
    DEVICE_EXEC_WORKERS,
    NETFLOW,
    PROMETHEUS,
    SYSLOG,
    TELEMETRY_SUBNET,
)
from nika.telemetry import management, netflow, prometheus, srl, syslog
from nika.telemetry.management import SCRIPT, TELEMETRY_COLLECTOR, address_plan
from nika.utils.parallel import bounded_parallel_map

_E1 = re.compile(r"^e1-(\d+)$")
SRL_EXPORTER = f"{CONFIG_DIR}/srl_exporter.py"
SRL_EXPORTER_PORT = 9100
_SRL_KINDS = frozenset({"nokia_srlinux", "srl_nokia", "srl"})


def used_e1_ports(name: str, links: list[dict[str, Any]]) -> set[int]:
    used: set[int] = set()
    prefix = f"{name}:"
    for link in links:
        for endpoint in link.get("endpoints") or []:
            if not isinstance(endpoint, str) or not endpoint.startswith(prefix):
                continue
            match = _E1.match(endpoint.split(":", 1)[1])
            if match:
                used.add(int(match.group(1)))
    return used


def next_e1_port(name: str, links: list[dict[str, Any]]) -> int:
    used = used_e1_ports(name, links)
    return max(used, default=0) + 1


def _next_mgmt_ip(
    network: ipaddress.IPv4Network | ipaddress.IPv6Network, used: set[str]
) -> str:
    gateway = str(network[1])
    for host in network.hosts():
        ip = str(host)
        if ip == gateway or ip in used:
            continue
        return ip
    raise ValueError(f"Telemetry collector has no free address in {network}")


def _srl_node_names(nodes: dict[str, Any]) -> list[str]:
    names = []
    for name, cfg in nodes.items():
        if name == TELEMETRY_COLLECTOR:
            continue
        kind = str((cfg or {}).get("kind") or "")
        if kind in _SRL_KINDS or kind.endswith("srlinux"):
            names.append(name)
    return sorted(names)


def inject_collector(
    doc: dict[str, Any],
    kinds: tuple[str, ...],
    *,
    gnmic: str | None,
) -> dict[str, Any]:
    """Add the collector (and sFlow links when ``netflow`` is requested) to ``doc``."""
    topology = doc.setdefault("topology", {})
    nodes = topology.setdefault("nodes", {})
    links = topology.setdefault("links", [])
    mgmt = doc.setdefault("mgmt", {})
    mgmt_subnet = ipaddress.ip_network(str(mgmt.get("ipv4-subnet") or "172.100.0.0/24"))
    used_v4 = {
        str(cfg.get("mgmt-ipv4"))
        for cfg in nodes.values()
        if isinstance(cfg, dict) and cfg.get("mgmt-ipv4")
    }
    collector_mgmt_ip = _next_mgmt_ip(mgmt_subnet, used_v4)
    node_cfg: dict[str, Any] = {
        "kind": "linux",
        "image": nika_image("telemetry-collector"),
        "mgmt-ipv4": collector_mgmt_ip,
        "memory": "1G",
    }
    if mgmt.get("ipv6-subnet"):
        mgmt_v6 = ipaddress.ip_network(str(mgmt["ipv6-subnet"]))
        used_v6 = {
            str(cfg.get("mgmt-ipv6"))
            for cfg in nodes.values()
            if isinstance(cfg, dict) and cfg.get("mgmt-ipv6")
        }
        node_cfg["mgmt-ipv6"] = _next_mgmt_ip(mgmt_v6, used_v6)
    if gnmic:
        node_cfg["binds"] = [f"{Path(gnmic).resolve()}:/usr/local/bin/gnmic:ro"]
    nodes[TELEMETRY_COLLECTOR] = node_cfg

    srl_names = _srl_node_names(nodes)
    devices: dict[str, Any] = {}
    collector_ifaces: list[str] = []
    flow_collector = ""
    flow_prefixlen = 0
    flow_addresses: dict[str, str] = {}
    if NETFLOW in kinds and srl_names:
        flow_collector, flow_prefixlen, flow_addresses = address_plan(
            TELEMETRY_SUBNET, srl_names
        )
        eth_index = 1
        for name in srl_names:
            e1 = next_e1_port(name, links)
            collector_iface = f"eth{eth_index}"
            links.append(
                {
                    "endpoints": [
                        f"{name}:{srl.clab_e1(e1)}",
                        f"{TELEMETRY_COLLECTOR}:{collector_iface}",
                    ]
                }
            )
            collector_ifaces.append(collector_iface)
            devices[name] = {
                "kind": "nokia_srlinux",
                "mgmt_ip": str(nodes[name].get("mgmt-ipv4") or ""),
                "srl_e1": e1,
                "data_e1": sorted(used_e1_ports(name, links) - {e1}),
                "collector_iface": collector_iface,
            }
            eth_index += 1
    for name in srl_names:
        devices.setdefault(
            name,
            {
                "kind": "nokia_srlinux",
                "mgmt_ip": str(nodes[name].get("mgmt-ipv4") or ""),
                "srl_e1": None,
                "data_e1": sorted(used_e1_ports(name, links)),
                "collector_iface": "",
            },
        )
    for name, cfg in nodes.items():
        if name == TELEMETRY_COLLECTOR or name in devices:
            continue
        devices[name] = {
            "kind": str((cfg or {}).get("kind") or "linux"),
            "mgmt_ip": str((cfg or {}).get("mgmt-ipv4") or ""),
            "srl_e1": None,
            "data_e1": [],
            "collector_iface": "",
        }
    return {
        "backend": "containerlab",
        "collector": TELEMETRY_COLLECTOR,
        "switch": "",
        "subnet": TELEMETRY_SUBNET,
        "mgmt_subnet": str(mgmt_subnet),
        "collector_mgmt_ip": collector_mgmt_ip,
        "collector_flow_ip": flow_collector,
        "flow_prefixlen": flow_prefixlen,
        "flow_addresses": flow_addresses,
        "collector_ifaces": collector_ifaces,
        "devices": devices,
        "addresses": {},
        "kinds": {kind: {"versions": {}} for kind in kinds},
    }


def attach(net_env: Any, kinds: tuple[str, ...]) -> dict[str, Any]:
    """Write the collector into the rendered clab topology before deploy."""
    from nika.utils.dependencies import require_gnmic

    require_gnmic()
    path = Path(net_env.topology_file)
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"Invalid Containerlab topology at {path}")
    plan = inject_collector(doc, kinds, gnmic=shutil.which("gnmic"))
    path.write_text(
        yaml.safe_dump(doc, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    net_env.declare_machine(
        TELEMETRY_COLLECTOR,
        role=NodeRole.INFRASTRUCTURE,
        capabilities=("linux", *kinds),
    )
    return plan


def configure(runtime: Any, plan: dict[str, Any]) -> dict[str, Any]:
    """Enable sFlow/gNMI exporters on SR Linux nodes that support them."""
    kinds = sorted(plan["kinds"])
    pairs = [(name, kind) for name in sorted(plan["devices"]) for kind in kinds]
    found = bounded_parallel_map(
        lambda pair: _probe(runtime, pair[0], pair[1]),
        pairs,
        max_workers=DEVICE_EXEC_WORKERS,
    )
    versions: dict[str, dict[str, str]] = {kind: {} for kind in kinds}
    for (name, kind), version in zip(pairs, found, strict=True):
        if version:
            versions[kind][name] = version
    flow_addresses = dict(plan.get("flow_addresses") or {})
    addresses = {
        name: flow_addresses.get(name)
        or str(plan["devices"][name].get("mgmt_ip") or "")
        for names in versions.values()
        for name in names
    }
    collector_flow = str(plan.get("collector_flow_ip") or "")
    prefixlen = int(plan.get("flow_prefixlen") or 16)

    if NETFLOW in versions:
        for name in versions[NETFLOW]:
            device = plan["devices"][name]
            e1 = device.get("srl_e1")
            if not e1:
                continue
            source = addresses[name]
            srl.enable_sflow(
                mgmt_ip=str(device["mgmt_ip"]),
                collector=collector_flow,
                source=source,
                telemetry_e1=int(e1),
                data_e1=[int(item) for item in device.get("data_e1") or []],
                prefix=f"{source}/{prefixlen}",
            )
    if SYSLOG in versions:
        collector_mgmt = str(plan.get("collector_mgmt_ip") or "")
        for name in versions[SYSLOG]:
            device = plan["devices"][name]
            mgmt_ip = str(device.get("mgmt_ip") or "")
            if collector_mgmt and mgmt_ip:
                srl.enable_syslog(mgmt_ip=mgmt_ip, collector=collector_mgmt)

    files: dict[str, str] = {}
    lines: list[str] = []
    hidden = ",".join(
        srl.yang_ethernet(int(plan["devices"][name]["srl_e1"]))
        for name, device in plan["devices"].items()
        if device.get("srl_e1")
    )
    if NETFLOW in kinds:
        files.update(
            netflow.collector_files(
                {name: addresses[name] for name in versions.get(NETFLOW, {})}
            )
        )
        lines += _sflow_collector_lines(
            plan.get("collector_ifaces") or [], collector_flow, prefixlen
        )
    if PROMETHEUS in kinds:
        mgmt_targets = {
            name: str(plan["devices"][name]["mgmt_ip"])
            for name in versions.get(PROMETHEUS, {})
        }
        files[SRL_EXPORTER] = srl.exporter_script()
        files.update(_prometheus_config())
        lines += prometheus.collector_lines()
        lines.append(
            "(while true; do "
            f"SRL_GNMI_USER={shlex.quote(srl.SRL_GNMI_USER)} "
            f"SRL_GNMI_PASSWORD={shlex.quote(srl.SRL_GNMI_PASSWORD)} "
            f"python3 {SRL_EXPORTER} 127.0.0.1 {SRL_EXPORTER_PORT} "
            f"{shlex.quote(json.dumps(mgmt_targets, separators=(',', ':')))} "
            f"{shlex.quote(hidden)}; sleep 1; done) &"
        )
    if SYSLOG in kinds:
        syslog_targets = {
            name: str(plan["devices"][name]["mgmt_ip"])
            for name in versions.get(SYSLOG, {})
        }
        files.update(
            syslog.collector_files(syslog_targets, versions=versions.get(SYSLOG))
        )
        lines += syslog.collector_lines(syslog_targets, versions=versions.get(SYSLOG))
    _run(runtime, plan["collector"], files, lines)
    return {
        **plan,
        "addresses": addresses,
        "kinds": {kind: {"versions": versions[kind]} for kind in kinds},
    }


def running(runtime: Any, plan: dict[str, Any]) -> bool:
    """Collector services are up and every SR Linux exporter is configured."""
    kinds = plan["kinds"]
    if NETFLOW in kinds:
        targets = {name: plan["addresses"][name] for name in kinds[NETFLOW]["versions"]}
        if not _netflow_running(runtime, plan["collector"], targets):
            return False
    if PROMETHEUS in kinds:
        targets = {
            name: plan["addresses"][name] for name in kinds[PROMETHEUS]["versions"]
        }
        if not prometheus.running(runtime, plan["collector"], targets):
            return False
    if SYSLOG in kinds:
        targets = {name: plan["addresses"][name] for name in kinds[SYSLOG]["versions"]}
        if not syslog.running(runtime, plan["collector"], targets):
            return False
    return True


def _probe(runtime: Any, name: str, kind: str) -> str | None:
    if kind == NETFLOW:
        return netflow.probe(runtime, name)
    if kind == SYSLOG:
        return srl.probe(runtime, name)
    return prometheus.probe(runtime, name)


def _sflow_collector_lines(
    ifaces: list[str], address: str, prefixlen: int
) -> list[str]:
    lines = [
        f"mkdir -p {netflow.FLOW_DIR}",
        "ip link add br-sflow type bridge stp_state 0 2>/dev/null || true",
    ]
    for iface in ifaces:
        lines.append(
            f"ip link set {iface} master br-sflow 2>/dev/null || true; "
            f"ip link set {iface} up"
        )
    if address:
        lines.append(f"ip addr replace {address}/{prefixlen} dev br-sflow")
        lines.append("ip link set br-sflow up")
    bind = f"-b {address} " if address else ""
    lines.append(
        f"sfcapd -D -w {netflow.FLOW_DIR} -t {netflow.FLOW_ROTATE_SEC} "
        f"{bind}-p {srl.SFLOW_PORT} -P /run/sfcapd.pid"
    )
    return lines


def _prometheus_config() -> dict[str, str]:
    config = {
        "global": {"scrape_interval": "15s"},
        "scrape_configs": [
            {
                "job_name": "srl",
                "scrape_timeout": "14s",
                "static_configs": [{"targets": [f"127.0.0.1:{SRL_EXPORTER_PORT}"]}],
            }
        ],
    }
    return {prometheus.PROMETHEUS_CONFIG: json.dumps(config, indent=1) + "\n"}


def _netflow_running(runtime: Any, collector: str, targets: dict[str, str]) -> bool:
    from nika.net_env.verify import process_running

    if not process_running(runtime, collector, "sfcapd"):
        return False
    if not targets:
        return True
    return all(
        bounded_parallel_map(
            lambda name: srl.sflow_enabled(runtime, name),
            sorted(targets),
            max_workers=DEVICE_EXEC_WORKERS,
        )
    )


def _run(runtime: Any, name: str, files: dict[str, str], lines: list[str]) -> None:
    dirs = sorted({CONFIG_DIR, *(posixpath.dirname(path) for path in files if path)})
    runtime.exec(name, f"mkdir -p {' '.join(dirs)}")
    for path, content in files.items():
        runtime.write_file(name, path, content)
    runtime.write_file(name, SCRIPT, "\n".join(["#!/bin/sh", *lines, "wait", ""]))
    runtime.exec(name, f"nohup sh {SCRIPT} >{management.LOG} 2>&1 &")
