"""Periodic active probes the recorder runs through baseline and incident.

The probe set is derived from the environment's declared inventory (hosts,
HTTP and DNS services), so every task of an environment probes the same
pairs at the same cadence.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from nika.net_env.probe_intent import ProbeIntent
from nika.runtime.spec import NodeRole
from nika.service.pingmesh import engine as pingmesh_engine
from nika.service.pingmesh.endpoints import resolve_endpoint_ip

CURL_FORMAT = (
    "code=%{http_code} dns=%{time_namelookup} connect=%{time_connect} "
    "ttfb=%{time_starttransfer} total=%{time_total} bytes=%{size_download} "
    "speed=%{speed_download}"
)
MAX_HTTP_CLIENTS = 3
MAX_DNS_NAMES = 8
IPERF_SECONDS = 3


@dataclass
class ProbePlan:
    clients: list[str]
    http_targets: dict[str, str]  # node -> IP
    dns_servers: dict[str, str]  # node -> IP
    dns_names: list[str]
    iperf_pairs: list[tuple[str, str]]
    intent: ProbeIntent | None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "clients": self.clients,
            "http_targets": self.http_targets,
            "dns_servers": self.dns_servers,
            "dns_names": self.dns_names,
            "iperf_pairs": [list(p) for p in self.iperf_pairs],
            "notes": self.notes,
        }


def _spread(items: list[str], k: int) -> list[str]:
    """Pick *k* items evenly spaced over the sorted list (deterministic)."""
    items = sorted(items)
    if len(items) <= k:
        return items
    step = (len(items) - 1) / (k - 1) if k > 1 else 0
    return sorted({items[round(i * step)] for i in range(k)})


def _dns_names(api: Any, server: str, ip: str, client: str) -> list[str]:
    """A-record names served by *server*, from a zone transfer done by *client*."""
    zones = api.exec_cmd(
        server,
        'named-checkconf -p 2>/dev/null | awk \'$1=="zone"{gsub(/"/,"",$2); print $2}\'',
        timeout=15,
    )
    names: list[str] = []
    for zone in zones.split():
        if (
            zone in (".", "localhost")
            or zone.endswith(".arpa")
            or not re.match(r"^[A-Za-z0-9_.-]+$", zone)
        ):
            continue
        out = api.exec_cmd(
            client, f"dig +time=2 +tries=1 @{ip} {zone} AXFR 2>&1", timeout=15
        )
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "A":
                names.append(parts[0].rstrip("."))
    return sorted(set(names))[:MAX_DNS_NAMES]


def build_plan(api: Any, inventory: Any, metadata: dict | None) -> ProbePlan:
    hosts = sorted(inventory.names_for_role(NodeRole.HOST)) if inventory else []

    def capable(cap: str) -> list[str]:
        if not inventory:
            return []
        return sorted(
            n
            for n in inventory.names_for_capability(cap)
            if inventory.to_dict()[n].get("role") != "infrastructure"
        )

    clients = _spread(hosts, MAX_HTTP_CLIENTS)
    notes: list[str] = []
    http_nodes = sorted(set(capable("http")) | set(capable("load_balancer")))
    http_targets = {n: ip for n in http_nodes if (ip := resolve_endpoint_ip(api, n))}
    dns_servers = {n: ip for n in capable("dns") if (ip := resolve_endpoint_ip(api, n))}
    dns_names: list[str] = []
    if dns_servers and clients:
        server, ip = next(iter(sorted(dns_servers.items())))
        try:
            dns_names = _dns_names(api, server, ip, clients[0])
        except Exception as exc:  # noqa: BLE001 - probes are best effort
            notes.append(f"dns name discovery failed: {exc}")
    targets = sorted(http_targets) or hosts
    iperf_pairs = [(c, t) for c in clients for t in _spread(targets, 2) if c != t][:4]
    if not clients:
        notes.append("no declared hosts: HTTP, DNS and iperf probes disabled")
    return ProbePlan(
        clients=clients,
        http_targets=http_targets,
        dns_servers=dns_servers,
        dns_names=dns_names,
        iperf_pairs=iperf_pairs,
        intent=ProbeIntent.from_metadata(metadata),
        notes=notes,
    )


def _parse_kv(text: str) -> dict[str, str]:
    return dict(re.findall(r"(\w+)=(\S+)", text))


class Prober:
    def __init__(self, api: Any, plan: ProbePlan, workers: int = 8) -> None:
        self.api = api
        self.plan = plan
        self.pool = ThreadPoolExecutor(max_workers=workers)

    def _exec(self, node: str, command: str, timeout: float) -> str:
        try:
            return self.api.exec_cmd(node, command, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - a failed probe is a result
            return f"probe_error: {exc}"

    def pingmesh(self) -> dict[str, Any]:
        snapshot = asyncio.run(
            pingmesh_engine.run_pingmesh_snapshot(
                self.api,
                count=3,
                max_pairs=64,
                intent=self.plan.intent,
                include_healthy=True,
            )
        )
        return json.loads(pingmesh_engine.snapshot_to_json(snapshot))

    def _http_one(self, client: str, target: str, ip: str) -> dict[str, Any]:
        out = self._exec(
            client,
            f"curl -s -o /dev/null -m 5 -w '{CURL_FORMAT}' http://{ip}/ 2>&1; echo",
            timeout=10,
        )
        return {
            "source": client,
            "target": target,
            "url": f"http://{ip}/",
            **_parse_kv(out),
            "raw": out.strip()[:300],
        }

    def _dns_one(
        self, client: str, server: str | None, ip: str | None, name: str
    ) -> dict[str, Any]:
        at = f"@{ip} " if ip else ""
        out = self._exec(
            client,
            f"dig +time=2 +tries=1 {at}{name} A 2>&1 | grep -E 'status:|Query time|^[^;].*\\sIN\\sA\\s' | head -5",
            timeout=10,
        )
        status = re.search(r"status: (\w+)", out)
        qtime = re.search(r"Query time: (\d+)", out)
        answers = re.findall(r"\sIN\sA\s+(\S+)", out)
        return {
            "source": client,
            "server": server or "resolv.conf",
            "name": name,
            "status": status.group(1) if status else "NO_REPLY",
            "query_ms": int(qtime.group(1)) if qtime else None,
            "answers": answers,
        }

    def http_dns_round(self) -> list[dict[str, Any]]:
        jobs = []
        for c in self.plan.clients:
            for t, ip in sorted(self.plan.http_targets.items()):
                jobs.append(("http", self.pool.submit(self._http_one, c, t, ip)))
            for name in self.plan.dns_names:
                jobs.append(
                    ("dns", self.pool.submit(self._dns_one, c, None, None, name))
                )
                for s, ip in sorted(self.plan.dns_servers.items()):
                    jobs.append(
                        ("dns", self.pool.submit(self._dns_one, c, s, ip, name))
                    )
        rows = []
        for kind, fut in jobs:
            rows.append({"kind": kind, **fut.result()})
        return rows

    def iperf(self, index: int) -> dict[str, Any] | None:
        if not self.plan.iperf_pairs:
            return None
        client, server = self.plan.iperf_pairs[index % len(self.plan.iperf_pairs)]
        start = time.time()
        try:
            out = self.api.iperf_test(
                client_host_name=client,
                server_host_name=server,
                duration=IPERF_SECONDS,
                client_args="-f m",
            )
        except Exception as exc:  # noqa: BLE001
            out = f"probe_error: {exc}"
        summary = re.findall(r"([\d.]+) Mbits/sec\s+(\d+)?\s*(sender|receiver)", out)
        return {
            "kind": "iperf",
            "source": client,
            "target": server,
            "seconds": IPERF_SECONDS,
            "sender_mbps": next(
                (float(m[0]) for m in summary if m[2] == "sender"), None
            ),
            "receiver_mbps": next(
                (float(m[0]) for m in summary if m[2] == "receiver"), None
            ),
            "retransmits": next(
                (int(m[1]) for m in summary if m[2] == "sender" and m[1]), None
            ),
            "elapsed": round(time.time() - start, 2),
            "raw_tail": "\n".join(out.strip().splitlines()[-4:]),
        }

    def traceroutes(self) -> list[dict[str, Any]]:
        jobs = []
        for c in self.plan.clients:
            for t, ip in sorted(self.plan.http_targets.items()):
                cmd = (
                    f"traceroute -n -w 1 -q 1 -m 12 {ip} 2>&1 || tracepath -n {ip} 2>&1"
                )
                jobs.append((c, t, self.pool.submit(self._exec, c, cmd, 30)))
        return [{"source": c, "target": t, "output": f.result()} for c, t, f in jobs]

    def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
