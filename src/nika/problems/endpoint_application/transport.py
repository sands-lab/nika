from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

from nika.net_env.verify import (
    http_download_stats,
    median_float,
    median_throughput_bps,
    ping_stats,
)
from nika.problems.base import (
    FailureDomain,
    ProblemBase,
    build_verify_result,
)
from nika.problems.rca import node_resource
from nika.problems.support.cpu_quota_helpers import (
    clear_original_nano_cpus,
    cpu_quota_to_nano_cpus,
    load_original_nano_cpus,
    persist_original_nano_cpus,
    read_nano_cpus,
    set_nano_cpus,
)
from nika.utils.logger import system_logger

_STRESS_CMD = (
    "nohup stress-ng --cpu 0 --cpu-load 100 --iomix 0 --sock 0 --hdd 2 "
    "--vm 0 --vm-bytes 75% --timeout {duration} </dev/null >/dev/null 2>&1 &"
)

_CPU_STRESS_CMD = (
    "nohup stress-ng --cpu {stress_cpus} --cpu-load 100 "
    "--timeout {duration} </dev/null >/dev/null 2>&1 &"
)

_DOCROOT = "/var/www"
_SMALL_OBJECT = f"{_DOCROOT}/small.bin"
_LARGE_OBJECT = f"{_DOCROOT}/large.bin"
_CPU_HTTP_SERVER = "/tmp/nika_cpu_http_server.py"

# Injected large-object performance vs baseline (either gate may pass).
# Target: unmistakable multi-x slowdown (order-of-magnitude class symptom).
_THROUGHPUT_MAX_RATIO = 0.20
_TIME_MIN_RATIO = 5.0
_MAX_LOSS_PERCENT = 5.0
_RECOVER_THROUGHPUT_MIN_RATIO = 0.70

# CPU-sensitive static file server: heavy per-chunk hashing so competing
# stress-ng under a tight CFS quota produces multi-x HTTP slowdown.
_CPU_HTTP_SERVER_SRC = f'''#!/usr/bin/env python3
"""CPU-sensitive file server for sender_resource_contention probes."""
from __future__ import annotations

import hashlib
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

DOCROOT = "{_DOCROOT}"
# Extra SHA256 rounds per 64 KiB. Each round hashes the full block (not the
# digest) so work scales with object size on modern CPUs.
ROUNDS_PER_64K_SMALL = 2
# Enough hashing that a 0.02-CPU CFS quota + stress-ng yields multi-x slowdown
# without making the healthy baseline a multi-minute download.
ROUNDS_PER_64K_LARGE = 160
LARGE_THRESHOLD = 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        rel = self.path.split("?", 1)[0]
        if rel in ("", "/"):
            rel = "/index.html"
        path = os.path.normpath(DOCROOT + rel)
        if not path.startswith(DOCROOT) or not os.path.isfile(path):
            self.send_error(404)
            return
        with open(path, "rb") as fh:
            data = fh.read()
        rounds = (
            ROUNDS_PER_64K_LARGE
            if len(data) >= LARGE_THRESHOLD
            else ROUNDS_PER_64K_SMALL
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        view = memoryview(data)
        step = 64 * 1024
        for i in range(0, len(data), step):
            block = bytes(view[i : i + step])
            digest = b""
            for _ in range(rounds):
                digest = hashlib.sha256(block + digest).digest()
            try:
                self.wfile.write(block)
            except (BrokenPipeError, ConnectionResetError):
                return

    def log_message(self, fmt: str, *args) -> None:
        return


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 80), Handler).serve_forever()
'''


# ==================================================================
# Problem: sender resource contention. Ref. Dapper: Data Plane Performance Diagnosis of TCP
# ==================================================================


class SenderResourceContentionParams(BaseModel):
    """Parameters for injecting HTTP-server CPU resource contention."""

    host_name: str = Field(description="Target HTTP server host name.")
    duration: int = Field(default=600, description="Stress duration in seconds.")
    cpu_quota: float = Field(
        default=0.05,
        description="Docker CPU quota applied to the HTTP server (fractional CPUs).",
    )
    stress_cpus: int = Field(
        default=16,
        description="Number of stress-ng CPU workers competing inside the quota.",
    )
    client_host: str = Field(
        default="client_0",
        description="HTTP client used for baseline and injected probes.",
    )
    dst_ip: str = Field(
        default="10.0.1.2",
        description="IPv4 address of the HTTP server (ping target).",
    )
    small_url: str = Field(
        default="http://web0.pod0/small.bin",
        description="Small-object URL (should remain roughly healthy).",
    )
    large_url: str = Field(
        default="http://web0.pod0/large.bin",
        description="Large-object URL used for bulk-transfer measurement.",
    )
    baseline_trials: int = Field(
        default=3,
        description="Number of large-object trials for median throughput/time.",
    )


class SenderResourceContention(ProblemBase):
    failure_domain = FailureDomain.ENDPOINT_APPLICATION
    root_cause_name: str = "sender_resource_contention"
    description = "Sender/server endpoint is under CPU resource contention."
    TAGS: list[str] = ["http"]
    Params = SenderResourceContentionParams
    symptom_desc = (
        "The HTTP server has insufficient CPU capacity under competing workload. "
        "Large or sustained HTTP transfers become slower while the network path, "
        "routing, packet loss, and basic TCP connectivity remain healthy."
    )

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._original_nano_cpus: int | None = None
        self._injected_nano_cpus: int | None = None
        self._baseline_throughput_bps: float | None = None
        self._baseline_time_s: float | None = None
        self._baseline_rtt_ms: float | None = None
        self._baseline_loss_percent: float | None = None

    def root_cause_resources(self, params: SenderResourceContentionParams):
        return [node_resource(params.host_name)]

    def parse_params(
        self, params: BaseModel | dict[str, Any] | None = None, **overrides: Any
    ) -> SenderResourceContentionParams:
        parsed = super().parse_params(params, **overrides)
        assert isinstance(parsed, SenderResourceContentionParams)
        return self._with_scenario_endpoints(parsed)

    def _with_scenario_endpoints(
        self, params: SenderResourceContentionParams
    ) -> SenderResourceContentionParams:
        """Fill client/URL defaults for Clos fabrics when cases omit them."""
        scenario = self.scenario_name or ""
        if scenario not in {"p4_dc_fabric", "sdn_l3_clos"}:
            return params
        model = getattr(self.net_env, "model", None)
        if model is None:
            return params
        webs = list(getattr(model, "web_endpoints", lambda: [])())
        clients = list(getattr(model, "client_endpoints", lambda: [])())
        if not webs:
            return params
        web = next((w for w in webs if w.name == params.host_name), webs[0])
        client = next(
            (c for c in clients if getattr(c, "leaf_id", None) != web.leaf_id),
            clients[0] if clients else None,
        )
        updates: dict[str, object] = {}
        if params.host_name != web.name:
            updates["host_name"] = web.name
        # Defaults in Params point at dc_clos names; rewrite for Clos fabrics.
        defaultish = (
            params.client_host in {"client_0", "client"}
            or "web0.pod0" in params.small_url
            or params.dst_ip in {"10.0.1.2", "200.0.0.8"}
        )
        if defaultish or not params.client_host:
            if client is not None:
                updates["client_host"] = client.name
            updates["dst_ip"] = web.ip
            updates["small_url"] = f"http://{web.ip}/small.bin"
            updates["large_url"] = f"http://{web.ip}/large.bin"
        if not updates:
            return params
        return params.model_copy(update=updates)

    def _ensure_http_objects(self, params: SenderResourceContentionParams) -> None:
        """Create objects and run a CPU-sensitive HTTP server on :80."""
        host = params.host_name
        # Always (re)write probe objects so a pre-existing tiny large.bin cannot
        # skip hashing (ROUNDS_PER_64K_LARGE only applies above 1 MiB).
        self.runtime.exec(
            host,
            (
                f"mkdir -p {_DOCROOT} && "
                f"dd if=/dev/zero of={_SMALL_OBJECT} bs=1024 count=16 "
                f"status=none 2>/dev/null || true; "
                f"dd if=/dev/zero of={_LARGE_OBJECT} bs=1M count=16 "
                f"status=none 2>/dev/null || true"
            ),
            timeout=120,
        )
        self.runtime.write_file(host, _CPU_HTTP_SERVER, _CPU_HTTP_SERVER_SRC)
        # Free :80. nika/nginx has no fuser/psmisc; stop nginx by name. Bracket
        # pkill patterns avoid matching the Kathara/docker exec shell argv.
        self.runtime.exec(
            host,
            "nginx -s stop 2>/dev/null || true; "
            "killall -9 nginx 2>/dev/null || true; "
            "pkill -x nginx 2>/dev/null || true; "
            "pkill -f '[p]ython3 /tmp/nika_cpu_http_server' 2>/dev/null || true; "
            "pkill -f '[p]ython3 -m http.server' 2>/dev/null || true; "
            "sleep 0.3",
            timeout=15,
        )
        start_out = self.runtime.exec(
            host,
            (
                f"nohup python3 {_CPU_HTTP_SERVER} </dev/null "
                f">/tmp/nika_cpu_http_server.log 2>&1 & echo START:$!"
            ),
            timeout=15,
        )
        deadline = time.time() + 25.0
        probe = ""
        while time.time() < deadline:
            time.sleep(0.5)
            probe = self.runtime.exec(
                host,
                "curl -s -o /dev/null -w '%{http_code}' --max-time 10 "
                "http://127.0.0.1/small.bin || true",
                timeout=20,
            ).strip()
            if probe in {"200", "206"}:
                break
        if probe not in {"200", "206"}:
            log = self.runtime.exec(
                host,
                "echo LOG; cat /tmp/nika_cpu_http_server.log 2>/dev/null || true; "
                "echo PS; ps aux 2>/dev/null | head -n 30 || true; "
                f"python3 -m py_compile {_CPU_HTTP_SERVER}; echo COMPILE:$?",
                timeout=20,
            )
            raise RuntimeError(
                f"CPU-sensitive HTTP server failed to serve on {host}: "
                f"http_code={probe!r} start_out={start_out!r} diag={log!r}"
            )

    def _median_large_stats(
        self,
        params: SenderResourceContentionParams,
        *,
        max_time_sec: int,
    ) -> tuple[float | None, float | None]:
        throughputs: list[float] = []
        times: list[float] = []
        for _ in range(params.baseline_trials):
            stats = http_download_stats(
                self.runtime,
                params.client_host,
                params.large_url,
                max_time_sec=max_time_sec,
            )
            if stats.ok and stats.throughput_bps is not None and stats.time_total_s:
                throughputs.append(stats.throughput_bps)
                times.append(stats.time_total_s)
        return median_float(throughputs), median_float(times)

    def measure_fault_degradation(
        self, params: SenderResourceContentionParams
    ) -> tuple[float | None, float | None, int]:
        """Measure enough faulted traffic to prove the configured slowdown."""
        baseline_bps = self._baseline_throughput_bps
        baseline_time = self._baseline_time_s
        if not baseline_bps or not baseline_time:
            return None, None, 0

        max_time_sec = max(20, min(180, int(baseline_time * (_TIME_MIN_RATIO + 1)) + 1))
        throughputs: list[float] = []
        times: list[float] = []
        for _ in range(2):
            stats = http_download_stats(
                self.runtime,
                params.client_host,
                params.large_url,
                max_time_sec=max_time_sec,
            )
            if stats.throughput_bps is None or not stats.time_total_s:
                continue
            throughputs.append(stats.throughput_bps)
            times.append(stats.time_total_s)
            if (
                stats.throughput_bps / baseline_bps <= _THROUGHPUT_MAX_RATIO
                or stats.time_total_s / baseline_time >= _TIME_MIN_RATIO
            ):
                break
        return median_float(throughputs), median_float(times), max_time_sec

    def inject_fault(self, params: SenderResourceContentionParams):
        self._ensure_http_objects(params)

        ping = ping_stats(
            self.runtime,
            params.client_host,
            params.dst_ip,
            count=10,
            interval_sec=0.2,
        )
        self._baseline_rtt_ms = ping.rtt_avg_ms
        self._baseline_loss_percent = ping.loss_percent

        bps, time_s = self._median_large_stats(params, max_time_sec=300)
        if bps is None or time_s is None:
            raise RuntimeError(
                "sender_resource_contention baseline large HTTP measurement failed "
                f"on {params.client_host} -> {params.large_url}"
            )
        self._baseline_throughput_bps = bps
        self._baseline_time_s = time_s

        original = read_nano_cpus(self.runtime, params.host_name)
        self._original_nano_cpus = original
        persist_original_nano_cpus(self.runtime, params.host_name, original)

        # Cap applied quota so selected Clos cases with 0.05 still contend hard.
        applied_quota = min(float(params.cpu_quota), 0.02)
        injected = cpu_quota_to_nano_cpus(applied_quota)
        set_nano_cpus(self.runtime, params.host_name, injected)
        self._injected_nano_cpus = injected

        # Restart the CPU-bound server under the new quota.
        self._ensure_http_objects(params)

        self.runtime.exec(
            params.host_name,
            _CPU_STRESS_CMD.format(
                stress_cpus=params.stress_cpus,
                duration=params.duration,
            ),
            timeout=15,
        )
        # Let CFS + stress-ng saturate before verify/symptom samples.
        deadline = time.time() + 15.0
        while time.time() < deadline:
            if self.runtime.process_running(params.host_name, "stress-ng"):
                break
            time.sleep(0.5)
        time.sleep(8.0)
        system_logger.info(
            "Injected sender_resource_contention on %s: "
            "cpu_quota=%.2f applied_quota=%.2f nano=%d stress_cpus=%d "
            "baseline_bps=%.0f baseline_time_s=%.3f rtt=%.2fms",
            params.host_name,
            params.cpu_quota,
            applied_quota,
            injected,
            params.stress_cpus,
            bps,
            time_s,
            ping.rtt_avg_ms or -1.0,
        )

    def verify_fault(self, params: SenderResourceContentionParams) -> dict:
        """Verify stress-ng and CPU quota are injected (artifact gate for inject)."""
        stress_running = self.runtime.process_running(params.host_name, "stress-ng")
        cpu_http_out = self.runtime.exec(
            params.host_name,
            "pgrep -af '[p]ython3 /tmp/nika_cpu_http_server' 2>/dev/null || true",
            timeout=10,
        ).strip()
        cpu_http_running = bool(cpu_http_out)
        current_nano = read_nano_cpus(self.runtime, params.host_name)
        expected_nano = self._injected_nano_cpus or cpu_quota_to_nano_cpus(
            min(float(params.cpu_quota), 0.02)
        )
        quota_ok = current_nano == expected_nano
        verified = bool(stress_running and quota_ok and cpu_http_running)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "client_host": params.client_host,
                "stress_running": stress_running,
                "cpu_http_running": cpu_http_running,
                "nano_cpus": current_nano,
                "expected_nano_cpus": expected_nano,
                "quota_ok": quota_ok,
            },
        )

    def recover_fault(self, params: SenderResourceContentionParams) -> dict:
        original = self._original_nano_cpus
        if original is None:
            original = load_original_nano_cpus(self.runtime, params.host_name)
        if original is None:
            return {
                "verified": False,
                "details": {"error": "original_nano_cpus_missing"},
            }

        self.runtime.exec(
            params.host_name,
            "pkill -f stress-ng 2>/dev/null || true",
            timeout=10,
        )
        time.sleep(0.5)
        set_nano_cpus(self.runtime, params.host_name, original)
        clear_original_nano_cpus(self.runtime, params.host_name)

        stress_gone = not self.runtime.process_running(params.host_name, "stress-ng")
        restored_nano = read_nano_cpus(self.runtime, params.host_name)
        quota_restored = restored_nano == original

        restored_bps = median_throughput_bps(
            self.runtime,
            params.client_host,
            params.large_url,
            trials=params.baseline_trials,
            max_time_sec=300,
        )
        baseline_bps = self._baseline_throughput_bps
        restore_ratio = None
        perf_restored = True
        if baseline_bps and restored_bps is not None:
            restore_ratio = restored_bps / baseline_bps
            perf_restored = restore_ratio >= _RECOVER_THROUGHPUT_MIN_RATIO

        ok = bool(stress_gone and quota_restored and perf_restored)
        system_logger.info(
            "recover_fault sender_resource_contention on %s: ok=%s "
            "nano=%d restore_ratio=%s",
            params.host_name,
            ok,
            restored_nano,
            restore_ratio,
        )
        return {
            "verified": ok,
            "details": {
                "host": params.host_name,
                "stress_gone": stress_gone,
                "restored_nano_cpus": restored_nano,
                "expected_nano_cpus": original,
                "quota_restored": quota_restored,
                "restored_throughput_bps": restored_bps,
                "baseline_throughput_bps": baseline_bps,
                "restore_ratio": restore_ratio,
                "perf_restored": perf_restored,
            },
        }


# ==================================================================
# Problem: receiver resource contention
# ==================================================================


class ReceiverResourceContentionParams(BaseModel):
    """Parameters for injecting a receiver resource contention fault."""

    host_name: str = Field(description="Target receiver host name.")
    duration: int = Field(default=600, description="Stress duration in seconds.")
    stress_cpus: int = Field(
        default=8,
        description="Number of stress-ng CPU workers on the receiver.",
    )
    peer_host: str = Field(
        default="web",
        description="HTTP server that sends a large object to the receiver.",
    )
    large_url: str = Field(
        default="",
        description="Large-object URL downloaded by the contended receiver.",
    )


class ReceiverResourceContention(ProblemBase):
    failure_domain = FailureDomain.ENDPOINT_APPLICATION
    root_cause_name: str = "receiver_resource_contention"
    description = "Receiver endpoint is under resource contention."
    TAGS: list[str] = ["http"]

    Params = ReceiverResourceContentionParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._baseline_throughput_bps: float | None = None
        self._baseline_time_s: float | None = None
        self._large_url: str | None = None

    def root_cause_resources(self, params: ReceiverResourceContentionParams):
        return [node_resource(params.host_name)]

    def _device_in_lab(self, name: str) -> bool:
        if not name:
            return False
        if name in set(self.net_env.hosts or []):
            return True
        if name in set(self.net_env.routers or []):
            return True
        servers = getattr(self.net_env, "servers", None) or {}
        for bucket in servers.values():
            if name in (bucket or []):
                return True
        return False

    def _resolve_peer_host(self, params: ReceiverResourceContentionParams) -> str:
        """Prefer an HTTP peer that exists in this scenario (dc_clos uses webserver*)."""
        peer = params.peer_host
        if self._device_in_lab(peer):
            return peer
        from nika.problems.support.probe_paths import get_probe_path

        path = get_probe_path(self.scenario_name or "")
        if path is not None and path.peer_host and self._device_in_lab(path.peer_host):
            return path.peer_host
        servers = getattr(self.net_env, "servers", None) or {}
        for name in servers.get("web") or []:
            if self._device_in_lab(name):
                return name
        return peer

    def _resolve_large_url(self, params: ReceiverResourceContentionParams) -> str:
        if params.large_url:
            return params.large_url
        from nika.problems.support.probe_paths import get_probe_path

        path = get_probe_path(self.scenario_name or "")
        if path is not None and path.http_url:
            base = path.http_url.rstrip("/")
            return f"{base}/large.bin"
        peer = self._resolve_peer_host(params)
        peer_ip = ""
        try:
            peer_ip = self.runtime.get_host_ip(peer, with_prefix=False) or ""
        except Exception:
            peer_ip = ""
        if not peer_ip:
            peer_ip = peer
        return f"http://{peer_ip}/large.bin"

    def _ensure_peer_large_object(
        self, params: ReceiverResourceContentionParams
    ) -> str:
        url = self._resolve_large_url(params)
        peer = self._resolve_peer_host(params)
        self.runtime.exec(
            peer,
            (
                "mkdir -p /var/www /usr/share/nginx/html /tmp 2>/dev/null || true; "
                "for d in /var/www /usr/share/nginx/html /tmp; do "
                "  dd if=/dev/zero of=$d/large.bin bs=1M count=16 status=none "
                "  2>/dev/null || true; "
                "done; "
                "curl -s -o /dev/null -w '%{http_code}' --max-time 3 "
                "http://127.0.0.1/large.bin 2>/dev/null | grep -qE '200|206' "
                "|| (pkill -f '[p]ython3 -m http.server 80' 2>/dev/null || true; "
                " cd /var/www && nohup python3 -m http.server 80 </dev/null "
                " >/tmp/nika_receiver_http.log 2>&1 & sleep 0.5)"
            ),
            timeout=60,
        )
        self._large_url = url
        return url

    def _median_large_stats(
        self,
        params: ReceiverResourceContentionParams,
        url: str,
        *,
        max_time_sec: int,
        trials: int = 3,
    ) -> tuple[float | None, float | None]:
        throughputs: list[float] = []
        times: list[float] = []
        for _ in range(trials):
            stats = http_download_stats(
                self.runtime,
                params.host_name,
                url,
                max_time_sec=max_time_sec,
            )
            if stats.ok and stats.throughput_bps is not None and stats.time_total_s:
                throughputs.append(stats.throughput_bps)
                times.append(stats.time_total_s)
        return median_float(throughputs), median_float(times)

    def inject_fault(self, params: ReceiverResourceContentionParams):
        url = self._ensure_peer_large_object(params)
        bps, time_s = self._median_large_stats(params, url, max_time_sec=120)
        if bps is None or time_s is None:
            raise RuntimeError(
                "receiver_resource_contention baseline large HTTP measurement "
                f"failed on {params.host_name} -> {url}"
            )
        self._baseline_throughput_bps = bps
        self._baseline_time_s = time_s

        # Stress-only: avoid Docker NanoCpus updates on llmd/k3s nodes where
        # clearing NanoCPUs back to unlimited is rejected or no-ops.
        self.runtime.exec(
            params.host_name,
            _CPU_STRESS_CMD.format(
                stress_cpus=params.stress_cpus,
                duration=params.duration,
            ),
            timeout=15,
        )
        self.runtime.exec(
            params.host_name,
            _STRESS_CMD.format(duration=params.duration),
            timeout=15,
        )
        deadline = time.time() + 15.0
        while time.time() < deadline:
            if self.runtime.process_running(params.host_name, "stress-ng"):
                break
            time.sleep(0.5)
        time.sleep(5.0)
        system_logger.info(
            "Injected TCP receiver resource contention on %s: "
            "stress_cpus=%d baseline_bps=%.0f url=%s",
            params.host_name,
            params.stress_cpus,
            bps,
            url,
        )

    def verify_fault(self, params: ReceiverResourceContentionParams) -> dict:
        """Verify stress-ng is running on the receiver."""
        pgrep_output = self.runtime.exec(
            params.host_name, "pgrep -a stress-ng 2>/dev/null || echo NONE"
        ).strip()
        stress_running = "stress-ng" in pgrep_output and pgrep_output != "NONE"
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=bool(stress_running),
            details={
                "host": params.host_name,
                "pgrep_output": pgrep_output,
                "stress_running": stress_running,
            },
        )

    def recover_fault(self, params: ReceiverResourceContentionParams) -> dict:
        self.runtime.exec(
            params.host_name,
            (
                "pkill -9 -f '[s]tress-ng' 2>/dev/null || true; "
                "pkill -9 stress-ng 2>/dev/null || true; "
                "killall -9 stress-ng 2>/dev/null || true"
            ),
            timeout=15,
        )
        time.sleep(1.0)
        stress_gone = not self.runtime.process_running(params.host_name, "stress-ng")
        return {
            "verified": bool(stress_gone),
            "details": {
                "host": params.host_name,
                "stress_gone": stress_gone,
            },
        }
