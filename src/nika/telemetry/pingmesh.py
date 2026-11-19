"""Always-on endpoint probes over the data plane, scraped over management."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from nika.net_env.verify import exec_or_empty, process_running
from nika.runtime.spec import NodeRole
from nika.telemetry import CONFIG_DIR, MGMT_VRF
from nika.telemetry import pingmesh_targets as targets

CONFIG = f"{CONFIG_DIR}/blackbox.json"
GENERATOR = f"{CONFIG_DIR}/pingmesh_targets.py"
MODULES = {
    "tcp_connect": {
        "prober": "tcp",
        "timeout": "12s",
        "tcp": {"preferred_ip_protocol": "ip4"},
    },
    "icmp": {
        "prober": "icmp",
        "timeout": "12s",
        "icmp": {"preferred_ip_protocol": "ip4"},
    },
    "http": {
        "prober": "http",
        "timeout": "12s",
        "http": {"preferred_ip_protocol": "ip4"},
    },
    "dns": {
        "prober": "dns",
        "timeout": "12s",
        "dns": {
            "preferred_ip_protocol": "ip4",
            "query_name": "localhost",
            "query_type": "A",
            # Authoritative servers can refuse names outside their zones.
            # This probe checks DNS responsiveness, not record correctness.
            "valid_rcodes": ["NOERROR", "NXDOMAIN", "REFUSED"],
        },
    },
}


def topology_plan(env) -> dict:
    """Capture declared endpoint identities and attachment points before deploy."""
    topology = (
        env._topology_segments()
        if getattr(env, "backend", None) != "containerlab"
        else env.get_topology()
    )
    endpoints = {}
    for name, identity in sorted(env.machine_identities.items()):
        if (
            identity.role not in {NodeRole.HOST, NodeRole.SERVICE}
            and not identity.reachability_target
        ):
            continue
        attachments = sorted(
            {
                other.split(":", 1)[0]
                for segment in topology
                for own in segment
                if own.split(":", 1)[0] == name
                for other in segment
                if other != own
            }
        )
        interfaces = sorted(
            {
                own.split(":", 1)[1]
                for segment in topology
                for own in segment
                if own.split(":", 1)[0] == name
            }
        )
        endpoints[name] = {
            "group": ",".join(attachments) or name,
            "interfaces": interfaces,
            "service": "dns"
            if "dns" in identity.capabilities
            else identity.service_type,
            "http": identity.role == NodeRole.SERVICE
            and "http" in identity.capabilities,
            "pod_image": getattr(env, "TRAFFIC_POD_IMAGE", None)
            if "k3s" in identity.capabilities
            else None,
        }
    intent = env.probe_intent()
    controllers = [
        name
        for name, identity in env.machine_identities.items()
        if identity.role == NodeRole.CONTROLLER and "k3s" in identity.capabilities
    ]
    return {
        "pod_controller": controllers[0] if controllers else None,
        "endpoints": endpoints,
        "isolation_partitions": intent.isolation_partitions if intent else [],
    }


def probe(runtime, name: str) -> str | None:
    out = exec_or_empty(
        runtime,
        name,
        "command -v prometheus-blackbox-exporter; command -v k3s; test -f /usr/local/lib/tcp_responder.py && echo responder",
    )
    if "/k3s" in out:
        return "k3s-blackbox" if "responder" in out else None
    return (
        "blackbox"
        if "prometheus-blackbox-exporter" in out and "responder" in out
        else None
    )


def endpoint_address(runtime, name: str, interfaces: list[str]) -> str | None:
    from nika.problems.support.oracle import is_service_ipv4

    raw = exec_or_empty(runtime, name, "ip -j -4 addr show")
    try:
        rows = json.loads(raw)
    except ValueError:
        return None
    for row in rows:
        if row.get("ifname") not in interfaces:
            continue
        for addr in row.get("addr_info", []):
            if addr.get("scope") == "global" and is_service_ipv4(addr.get("local", "")):
                return addr["local"]
    return None


def device_files(
    device: dict, address: str, collector: str, version: str = ""
) -> dict[str, str]:
    return {CONFIG: json.dumps({"modules": MODULES})}


def device_lines(device: dict, address: str, version: str = "") -> list[str]:
    # The responder binds only the data address; probes originate outside the management VRF.
    data = shlex.quote(device["probe_address"])
    bind_device = f",so-bindtodevice={MGMT_VRF}" if device.get("mgmt_iface") else ""
    lines = [
        f"(while true; do python3 /usr/local/lib/tcp_responder.py {data}; sleep 1; done) &",
        f"(while true; do prometheus-blackbox-exporter --config.file={CONFIG} --web.listen-address=127.0.0.1:{targets.PROBER_PORT}; sleep 1; done) &",
        f"(while true; do socat TCP-LISTEN:{targets.PROBER_PORT},bind={address}{bind_device},reuseaddr,fork TCP:127.0.0.1:{targets.PROBER_PORT}; sleep 1; done) &",
    ]

    return lines[2:] if version == "k3s-blackbox" else lines


def collector_files(
    endpoints: dict, partitions=(), declared_endpoints=()
) -> dict[str, str]:
    plan = {
        "endpoints": endpoints,
        "isolation_partitions": partitions,
        "declared_endpoints": list(declared_endpoints or endpoints),
    }
    return {
        targets.PLAN_FILE: json.dumps(plan),
        targets.TARGET_FILE: "[]",
        GENERATOR: Path(targets.__file__).read_text(),
    }


def scrape_job() -> dict:
    return {
        "job_name": "pingmesh",
        "scrape_interval": f"{targets.INTERVAL}s",
        "scrape_timeout": "14s",
        "metrics_path": "/probe",
        "honor_labels": True,
        "http_sd_configs": [
            {
                "url": f"http://127.0.0.1:{targets.STATUS_PORT}/targets",
                "refresh_interval": "1s",
            }
        ],
    }


def collector_lines(*_args, **_kwargs) -> list[str]:
    return [f"(while true; do python3 {GENERATOR}; sleep 1; done) &"]


def running(runtime, collector: str, exporters: dict[str, str]) -> bool:
    return (
        process_running(runtime, collector, "prometheus")
        and bool(
            exec_or_empty(
                runtime,
                collector,
                "curl -fsS --max-time 3 http://127.0.0.1:9103/metrics",
            )
        )
        and all(
            exec_or_empty(
                runtime,
                name,
                "python3 -c 'import urllib.request; "
                f'print(urllib.request.urlopen("http://127.0.0.1:{targets.PROBER_PORT}/-/healthy", '
                "timeout=3).status)'",
            ).strip()
            == "200"
            for name in exporters
        )
        and (not exporters or _scrapes_running(runtime, collector))
    )


def _scrapes_running(runtime, collector: str) -> bool:
    """Check completed scrapes; a rotated target can await its first sample."""
    raw = exec_or_empty(
        runtime,
        collector,
        "curl -fsS --max-time 3 'http://127.0.0.1:9090/api/v1/targets?state=active'",
    )
    try:
        rows = json.loads(raw)["data"]["activeTargets"]
    except (ValueError, KeyError, TypeError):
        return False
    probes = [
        row
        for row in rows
        if row.get("labels", {}).get("job") == "pingmesh"
        and row.get("health") != "unknown"
    ]
    return bool(probes) and all(row.get("health") == "up" for row in probes)


def start_kubernetes_probers(runtime, controller: str, endpoints: dict) -> None:
    """Run cached blackbox and responder containers in host-network pods."""
    import base64
    from nika.net_env.utils.k8s_workload_cache import BLACKBOX_EXPORTER_IMAGE

    items = []
    for name, endpoint in endpoints.items():
        if not endpoint.get("pod_image"):
            continue
        mount = [{"name": "config", "mountPath": CONFIG_DIR, "readOnly": True}]
        items.append(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": f"endpoint-probe-{name}",
                    "namespace": "kube-system",
                },
                "spec": {
                    "nodeName": name,
                    "hostNetwork": True,
                    "tolerations": [{"operator": "Exists"}],
                    "containers": [
                        {
                            "name": "probe",
                            "image": BLACKBOX_EXPORTER_IMAGE,
                            "imagePullPolicy": "Never",
                            "args": [
                                f"--config.file={CONFIG}",
                                f"--web.listen-address=127.0.0.1:{targets.PROBER_PORT}",
                            ],
                            "securityContext": {
                                "runAsUser": 0,
                                "capabilities": {"add": ["NET_RAW"]},
                            },
                            "volumeMounts": mount,
                            "readinessProbe": {
                                "tcpSocket": {
                                    "port": targets.PROBER_PORT,
                                    "host": "127.0.0.1",
                                }
                            },
                            "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}},
                        },
                        {
                            "name": "responder",
                            "image": endpoint["pod_image"],
                            "imagePullPolicy": "Never",
                            "command": [
                                "python3",
                                "/opt/probe/tcp_responder.py",
                                endpoint["address"],
                            ],
                            "volumeMounts": [
                                {
                                    "name": "responder",
                                    "mountPath": "/opt/probe/tcp_responder.py",
                                    "readOnly": True,
                                }
                            ],
                            "resources": {"requests": {"cpu": "1m", "memory": "8Mi"}},
                        },
                    ],
                    "volumes": [
                        {"name": "config", "hostPath": {"path": CONFIG_DIR}},
                        {
                            "name": "responder",
                            "hostPath": {"path": "/usr/local/lib/tcp_responder.py"},
                        },
                    ],
                },
            }
        )
    if not items:
        return
    encoded = base64.b64encode(
        json.dumps({"apiVersion": "v1", "kind": "List", "items": items}).encode()
    ).decode()
    runtime.exec(
        controller,
        f"printf %s {shlex.quote(encoded)} | base64 -d | kubectl apply -f -",
        timeout=30,
    )
    pods = " ".join(
        "endpoint-probe-" + name for name, e in endpoints.items() if e.get("pod_image")
    )
    runtime.exec(
        controller,
        f"kubectl -n kube-system wait --for=condition=Ready --timeout=120s pod {pods}",
        timeout=130,
    )


def host_budget_directory() -> Path:
    """Use local, user-private leases shared by collectors on the same host."""
    import os
    import stat

    directory = Path("/tmp") / f"endpoint-probe-budget-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise RuntimeError(f"Probe budget directory is not private: {directory}")
    return directory


def status_job() -> dict:
    return {
        "job_name": "pingmesh_status",
        "static_configs": [{"targets": [f"127.0.0.1:{targets.STATUS_PORT}"]}],
    }
