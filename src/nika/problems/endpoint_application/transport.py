from __future__ import annotations

import json
import re
import time
from dataclasses import asdict
from typing import Any
from urllib.parse import urlsplit

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
from nika.problems.support.benchmark_targets import endpoint_target, first
from nika.problems.support.recovery import FileSnapshot
from nika.service.lab.checked_exec import checked_exec
from nika.problems.support.oracle import (
    Check,
    OraclePlaybook,
    OracleStep,
    ping,
    shell,
)
from nika.problems.support.cpu_quota_helpers import (
    container_commands,
    cpu_quota_to_nano_cpus,
    read_nano_cpus,
    set_nano_cpus,
)
from nika.traffic.workload import BULK_DOWNLOAD
from nika.utils.logger import system_logger

# The environment stages an idle archive batch on every endpoint. Injection
# starts its gzip workers over the local snapshot corpus.
_ARCHIVE_JOB = "/usr/local/bin/archive-job.py"
_ARCHIVE_CONFIG = "/etc/archive-job.json"
_ARCHIVE_LOG = "/var/log/archive-job.log"
_ARCHIVE_MAX_WORKERS = 16


def _stage_archive_job(runtime: Any, host: str, workers: int) -> dict[str, Any]:
    """Set the worker count of the environment's idle archive batch."""
    config = FileSnapshot.capture(runtime, host, _ARCHIVE_CONFIG).content
    if config is None:
        raise RuntimeError(
            f"Archive configuration {_ARCHIVE_CONFIG} is absent on {host}"
        )
    runtime.write_file(
        host,
        _ARCHIVE_CONFIG,
        json.dumps({"workers": max(1, min(_ARCHIVE_MAX_WORKERS, workers))}) + "\n",
    )
    return {"config": config}


def _launch_archive_job(runtime: Any, host: str) -> None:
    """Start the batch without waiting inside a CPU-starved container."""
    runtime.exec(
        host,
        f"nohup setsid python3 {_ARCHIVE_JOB} run --duration 0 "
        f"</dev/null >>{_ARCHIVE_LOG} 2>&1 &",
        timeout=15,
    )


def _archive_job_running(commands: list[str]) -> bool:
    """Whether the batch driver and at least one gzip worker are running."""
    return any(f"{_ARCHIVE_JOB} run" in cmd for cmd in commands) and any(
        cmd.split()[:1] == ["gzip"] for cmd in commands
    )


def _remove_archive_job(runtime: Any, host: str, staged: dict[str, Any]) -> None:
    """Stop the batch and restore the files staged by ``_stage_archive_job``."""
    runtime.exec(
        host,
        f"python3 {_ARCHIVE_JOB} stop 2>/dev/null; "
        "pkill -f '[a]rchive-job.py run' 2>/dev/null; true",
        timeout=30,
    )
    if staged.get("config") is not None:
        runtime.write_file(host, _ARCHIVE_CONFIG, staged["config"])


def _gzip_workers(output: str) -> int:
    return len(re.findall(r"^\s*\S+\s+[0-9.]+\s+gzip\b", output, re.MULTILINE))


def _cpu_throttled(output: str) -> bool:
    """cgroup ``cpu.max`` grants under a quarter CPU and the group was throttled."""
    quota = re.search(r"^(\d+)\s+(\d+)$", output, re.MULTILINE)
    throttled = re.search(r"^nr_throttled\s+(\d+)", output, re.MULTILINE)
    return (
        quota is not None
        and int(quota.group(1)) / int(quota.group(2)) < 0.25
        and throttled is not None
        and int(throttled.group(1)) > 0
    )


# CPU share of the contended receiver. The archive workers and the download
# split it, which roughly halves the download rate, while a command run on the
# node still finishes in under half a minute.
_RECEIVER_CPU_QUOTA = 0.05


def _cpu_contention_steps(host: str, *, quota_evidence: bool) -> list[OracleStep]:
    """Competing CPU-bound processes and CFS throttling on ``host``.

    A starved container answers ``exec`` slowly, so the reads get long
    timeouts and retries.
    """
    return [
        shell(
            host,
            "ps -eo stat,pcpu,comm,args --sort=-pcpu | head -25",
            f"List the busiest processes on {host}.",
            Check(
                "two or more CPU-bound gzip workers run on the node",
                lambda out: _gzip_workers(out) >= 2,
            ),
            evidence=True,
            timeout=60,
            attempts=3,
        ),
        shell(
            host,
            "cat /sys/fs/cgroup/cpu.max /sys/fs/cgroup/cpu.stat 2>&1 | head -12",
            f"Read the CPU allowance and throttling counters of {host}.",
            Check(
                "the node's cgroup gets under a quarter CPU and is being throttled",
                _cpu_throttled,
            ),
            evidence=quota_evidence,
            timeout=60,
            attempts=3,
        ),
    ]


def _download_probe(client: str, url: str, purpose: str) -> OracleStep:
    return shell(
        client,
        "curl -s -o /dev/null --max-time 90 -w 'code=%{http_code} "
        f"speed=%{{speed_download}} time=%{{time_total}}\\n' {url}",
        purpose,
        timeout=100,
    )


# Injected large-object performance vs baseline (either gate may pass).
# Target: unmistakable multi-x slowdown (order-of-magnitude class symptom).
_THROUGHPUT_MAX_RATIO = 0.20
_TIME_MIN_RATIO = 5.0
_MAX_LOSS_PERCENT = 5.0
_RECOVER_THROUGHPUT_MIN_RATIO = 0.70

# ==================================================================
# Problem: sender resource contention. Ref. Dapper: Data Plane Performance Diagnosis of TCP
# ==================================================================


class SenderResourceContentionParams(BaseModel):
    """Parameters for injecting HTTP-server CPU resource contention."""

    host_name: str = Field(description="Target HTTP server host name.")
    duration: int = Field(
        default=3600,
        description="Legacy duration parameter; contention persists until recovery.",
    )
    cpu_quota: float = Field(
        default=0.05,
        description="Docker CPU quota applied to the HTTP server (fractional CPUs).",
    )
    stress_cpus: int = Field(
        default=16,
        description="Number of CPU-bound archive workers competing inside the quota.",
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
    symptom_probe = "custom"
    symptom_class = "degradation"
    dynamic_artifact = True
    root_cause_name: str = "sender_resource_contention"
    # A sender held to a 5% CPU quota still sends 50-70 kbit/s, so the path
    # must carry well above that for the transfer to fall to a fifth of it.
    minimum_path_rate_kbps = 1_000
    description = (
        "The host that sends the data (the serving end of a transfer) is "
        "CPU-starved, so it produces data slowly."
    )
    TAGS: list[str] = ["http"]
    Params = SenderResourceContentionParams
    symptom_desc = (
        "The HTTP server has insufficient CPU capacity under competing workload. "
        "Large or sustained HTTP transfers become slower while the network path, "
        "routing, packet loss, and basic TCP connectivity remain healthy."
    )
    BENCHMARK_TARGETS = "canonical"
    # The slowdown shows in the downloads the starved server sends to its client.
    SYMPTOM_WORKLOAD = BULK_DOWNLOAD

    @classmethod
    def symptom_workload_target(cls, inject):
        return (inject.get("client_host") or "",)

    @classmethod
    def benchmark_inject_params(cls, ctx):
        # Prefer real HTTP servers over client pools that some scenarios label "web".
        scenario, net_env = ctx.scenario, ctx.net_env
        host_pool, host0, web0 = ctx.host_pool, ctx.host0, ctx.web0
        params: dict[str, str] = {}
        web_pool = list(ctx.servers.get("web") or []) or list(
            ctx.roles.get("web") or []
        )
        if scenario == "enterprise_branch":
            params["host_name"] = (
                "hq_srv"
                if "hq_srv" in web_pool
                else (web0 if web0 in web_pool else (first(web_pool) or host0))
            )
            params["client_host"] = (
                "br1_corp_pc" if "br1_corp_pc" in host_pool else host0
            )
            params["dst_ip"] = "10.0.20.2"
            params["small_url"] = "http://10.0.20.2/small.bin"
            params["large_url"] = "http://10.0.20.2/large.bin"
            params["cpu_quota"] = "0.05"
            params["stress_cpus"] = "16"
        elif scenario == "campus_lan":
            params["host_name"] = (
                "web_server_0"
                if "web_server_0" in web_pool
                else (web0 if web0 in web_pool else (first(web_pool) or host0))
            )
            params["client_host"] = "pc_1_1_1_1" if "pc_1_1_1_1" in host_pool else host0
            params["dst_ip"] = "10.200.0.3"
            # Campus web endpoints serve static files on :8080.
            params["small_url"] = "http://10.200.0.3:8080/small.bin"
            params["large_url"] = "http://10.200.0.3:8080/large.bin"
            params["cpu_quota"] = "0.05"
            params["stress_cpus"] = "16"
        elif scenario == "llmd_lab":
            params["host_name"] = (
                "web" if "web" in web_pool else (first(web_pool) or host0)
            )
            params["client_host"] = "client" if "client" in host_pool else host0
            params["dst_ip"] = "200.0.0.8"
            params["small_url"] = "http://200.0.0.8/small.bin"
            params["large_url"] = "http://200.0.0.8/large.bin"
            params["cpu_quota"] = "0.05"
            params["stress_cpus"] = "16"
        elif scenario in {"dc_clos", "p4_dc_fabric", "sdn_l3_clos"}:
            model = getattr(net_env, "model", None)
            webs = list(getattr(model, "web_endpoints", lambda: [])()) if model else []
            clients = (
                list(getattr(model, "client_endpoints", lambda: [])()) if model else []
            )
            web = webs[0] if webs else None
            if web is None:
                params["host_name"] = (
                    web0 if web0 in web_pool else (first(web_pool) or host0)
                )
            else:
                client = next(
                    (c for c in clients if c.leaf_id != web.leaf_id),
                    clients[0] if clients else None,
                )
                params["host_name"] = web.name
                if client is not None:
                    params["client_host"] = client.name
                params["dst_ip"] = web.ip
                params["small_url"] = f"http://{web.ip}/small.bin"
                params["large_url"] = f"http://{web.ip}/large.bin"
                params["cpu_quota"] = "0.05"
                params["stress_cpus"] = "16"
        else:
            params["host_name"] = (
                web0 if web0 in web_pool else (first(web_pool) or host0)
            )
        params["duration"] = "3600"
        return params

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._original_nano_cpus: int | None = None
        self._injected_nano_cpus: int | None = None
        self._baseline_throughput_bps: float | None = None
        self._baseline_time_s: float | None = None
        self._baseline_rtt_ms: float | None = None
        self._baseline_loss_percent: float | None = None
        self._archive: dict[str, Any] | None = None

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
        if scenario not in {"dc_clos", "p4_dc_fabric", "sdn_l3_clos"}:
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
        # Resolve legacy parameter defaults from the declared Clos endpoints.
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
        # A starved sender host can also drop the exec that runs curl; such an
        # attempt yields no measurement, so allow one more.
        for attempt in range(3):
            if attempt == 2 and throughputs:
                break
            stats = http_download_stats(
                self.runtime,
                params.client_host,
                params.large_url,
                max_time_sec=max_time_sec,
            )
            # A starved server may not send headers before the deadline; that
            # timeout is itself a measured slowdown.
            timed_out = (stats.time_total_s or 0) >= max_time_sec
            if stats.throughput_bps is None and not timed_out:
                continue
            bps = stats.throughput_bps or (stats.size_bytes or 0) * 8.0 / max_time_sec
            throughputs.append(bps)
            times.append(stats.time_total_s)
            if (
                bps / baseline_bps <= _THROUGHPUT_MAX_RATIO
                or stats.time_total_s / baseline_time >= _TIME_MIN_RATIO
            ):
                break
        return median_float(throughputs), median_float(times), max_time_sec

    def inject_fault(self, params: SenderResourceContentionParams):
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

        # Stage the batch while the node still has CPU to spare.
        self._archive = _stage_archive_job(
            self.runtime, params.host_name, params.stress_cpus
        )
        original = read_nano_cpus(self.runtime, params.host_name)
        self._original_nano_cpus = original

        applied_quota = float(params.cpu_quota)
        injected = cpu_quota_to_nano_cpus(applied_quota)
        set_nano_cpus(self.runtime, params.host_name, injected)
        self._injected_nano_cpus = injected

        _launch_archive_job(self.runtime, params.host_name)
        # Let CFS + the archive workers saturate before verify/symptom samples.
        deadline = time.time() + 30.0
        while time.time() < deadline:
            if _archive_job_running(container_commands(self.runtime, params.host_name)):
                break
            time.sleep(0.5)
        time.sleep(8.0)
        system_logger.info(
            "Injected sender_resource_contention on %s: "
            "cpu_quota=%.2f applied_quota=%.2f nano=%d workers=%d "
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
        """Verify the archive batch and CPU quota are injected (artifact gate)."""
        commands = container_commands(self.runtime, params.host_name)
        stress_running = _archive_job_running(commands)
        port = urlsplit(params.large_url).port or 80
        listeners = checked_exec(
            self.runtime.exec,
            params.host_name,
            f"ss -Hltn 'sport = :{port}'",
            timeout=120,
        )
        http_running = any(
            line.split()[:1] == ["LISTEN"] for line in listeners.splitlines()
        )
        current_nano = read_nano_cpus(self.runtime, params.host_name)
        expected_nano = self._injected_nano_cpus or cpu_quota_to_nano_cpus(
            float(params.cpu_quota)
        )
        quota_ok = current_nano == expected_nano
        verified = bool(stress_running and quota_ok and http_running)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "client_host": params.client_host,
                "workload_running": stress_running,
                "http_running": http_running,
                "nano_cpus": current_nano,
                "expected_nano_cpus": expected_nano,
                "quota_ok": quota_ok,
            },
        )

    def oracle_playbook(self, params: SenderResourceContentionParams) -> OraclePlaybook:
        server, client = params.host_name, params.client_host
        steps = [
            ping(
                client,
                params.dst_ip,
                f"Ping the server {server} from {client}: the path has no loss "
                "and normal latency.",
                count=5,
            ),
            _download_probe(
                client, params.small_url, "Fetch the small object from the server."
            ),
            _download_probe(
                client,
                params.large_url,
                "Fetch the large object: the bulk transfer the server sends.",
            ),
            shell(
                client,
                "cat /sys/fs/cgroup/cpu.max 2>&1; uptime",
                f"Check that the receiving client {client} has CPU to spare.",
            ),
            *_cpu_contention_steps(server, quota_evidence=True),
        ]
        return OraclePlaybook(
            finding=(
                f"The sending server {server} is CPU-starved: CPU-bound batch "
                "workers share its small CPU allowance, so it produces the "
                "response data slowly while the network path is healthy."
            ),
            steps=steps,
        )

    def recover_fault(self, params: SenderResourceContentionParams) -> dict:
        # Restore state lives in this Problem instance, never on the node.
        original = self._original_nano_cpus
        if original is None:
            return {
                "verified": False,
                "details": {"error": "original_nano_cpus_missing"},
            }

        # Give the node its CPU back first so the batch stops promptly.
        set_nano_cpus(self.runtime, params.host_name, original)
        _remove_archive_job(self.runtime, params.host_name, self._archive or {})
        deadline = time.monotonic() + 8.0
        while (
            stress_running := _archive_job_running(
                container_commands(self.runtime, params.host_name)
            )
        ) and time.monotonic() < deadline:
            time.sleep(0.25)
        stress_gone = not stress_running
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

        stress_gone = stress_gone and not _archive_job_running(
            container_commands(self.runtime, params.host_name)
        )
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
                "workload_gone": stress_gone,
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

_CAMPUS_CATALOG_URL = "https://web0.local/packages/catalog.txt"
_CAMPUS_CATALOG_BYTES = 64 * 1024 * 1024


class ReceiverResourceContentionParams(BaseModel):
    """Parameters for injecting a receiver resource contention fault."""

    host_name: str = Field(description="Target receiver host name.")
    duration: int = Field(
        default=3600,
        description="Legacy duration parameter; contention persists until recovery.",
    )
    stress_cpus: int = Field(
        default=8,
        description="Number of CPU-bound archive workers on the receiver.",
    )
    archive_workers: int = Field(
        default=8,
        ge=2,
        le=16,
        description="Concurrent local gzip archive workers on campus_lan.",
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
    symptom_probe = "custom"
    symptom_class = "degradation"
    dynamic_artifact = True
    root_cause_name: str = "receiver_resource_contention"
    description = (
        "The host that receives the data (the consuming end of a transfer) is "
        "CPU-starved, so it reads incoming data slowly."
    )
    TAGS: list[str] = ["http"]

    Params = ReceiverResourceContentionParams
    # The slowdown shows in the downloads the starved receiver reads.
    SYMPTOM_WORKLOAD = BULK_DOWNLOAD
    # A starved receiver closes its window only when the path delivers faster
    # than it reads; a 1 Mbit/s RouterOS path is slower than such a reader.
    minimum_path_rate_kbps = 10_000

    @classmethod
    def symptom_workload_target(cls, inject):
        return (inject.get("host_name") or "",)

    @classmethod
    def benchmark_inject_params(cls, ctx):
        params = endpoint_target(ctx)
        params["duration"] = "3600"
        if ctx.scenario == "dc_clos":
            _, service, _ = ctx.net_env.model.probe_endpoints(params.get("host_name"))
            params["peer_host"] = service.name
            params["large_url"] = f"http://{service.ip}/large.bin"
            params["stress_cpus"] = "4"
        elif ctx.scenario == "campus_lan":
            params["peer_host"] = "web_server_0"
            params["large_url"] = _CAMPUS_CATALOG_URL
        elif ctx.scenario in {"p4_dc_fabric", "llmd_lab"}:
            params["stress_cpus"] = "16"
        return params

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._baseline_throughput_bps: float | None = None
        self._baseline_time_s: float | None = None
        self._large_url: str | None = None
        self._last_downloads: list[dict] = []
        self._control_baseline_bps: float | None = None
        self._archive_pid: int | None = None
        self._archive_config: str | None = None
        self._archive: dict[str, Any] | None = None

    def _archive_job(self, host: str, action: str) -> dict:
        output = self.runtime.exec(
            host, f"python3 /usr/local/bin/archive-job.py {action}", timeout=20
        ).strip()
        return json.loads(output)

    def _control_host(self, params: ReceiverResourceContentionParams) -> str:
        return next(
            host
            for host in self.net_env.hosts
            if host.startswith("pc_") and host != params.host_name
        )

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

    def _resolve_peer_hosts(
        self, params: ReceiverResourceContentionParams
    ) -> list[str]:
        """HTTP servers that must hold the large object."""
        if self._device_in_lab(params.peer_host):
            return [params.peer_host]
        servers = getattr(self.net_env, "servers", None) or {}
        return [
            name
            for name in servers.get("web") or []
            if name != params.host_name and self._device_in_lab(name)
        ]

    def _resolve_large_url(self, params: ReceiverResourceContentionParams) -> str:
        if params.large_url:
            return params.large_url
        if self.scenario_name == "campus_lan":
            return _CAMPUS_CATALOG_URL
        from nika.net_env.net_env_pool import get_probe_path

        path = get_probe_path(self.scenario_name or "", net_env=self.net_env)
        if path is not None and path.http_url:
            parts = urlsplit(path.http_url)
            base = parts.path.rsplit("/", 1)[0]
            return f"{parts.scheme}://{parts.netloc}{base}/large.bin"
        peers = self._resolve_peer_hosts(params)
        peer = peers[0] if peers else params.peer_host
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
        self._large_url = url
        return url

    def _median_large_stats(
        self,
        params: ReceiverResourceContentionParams,
        url: str,
        *,
        max_time_sec: int,
        trials: int = 3,
        host: str | None = None,
    ) -> tuple[float | None, float | None]:
        throughputs: list[float] = []
        times: list[float] = []
        self._last_downloads = []
        campus = self.scenario_name == "campus_lan"
        host = host or params.host_name
        for _ in range(trials):
            output_path = (
                self.runtime.exec(host, "mktemp /run/catalog.XXXXXX").strip()
                if campus
                else "/dev/null"
            )
            try:
                stats = http_download_stats(
                    self.runtime,
                    host,
                    url,
                    max_time_sec=max_time_sec,
                    output_path=output_path,
                    compressed=campus,
                )
                sample = asdict(stats)
                self._last_downloads.append(sample)
                if campus:
                    decoded = self.runtime.exec(
                        host, f"stat -c %s {output_path} 2>/dev/null"
                    ).strip()
                    sample["decoded_bytes"] = (
                        int(decoded) if decoded.isdigit() else None
                    )
                    if (
                        not stats.ok
                        or stats.exit_code != 0
                        or sample["decoded_bytes"] != _CAMPUS_CATALOG_BYTES
                    ):
                        return None, None
                if stats.ok and stats.throughput_bps is not None and stats.time_total_s:
                    throughputs.append(stats.throughput_bps)
                    times.append(stats.time_total_s)
            finally:
                if campus:
                    self.runtime.exec(host, f"rm -f {output_path}")
        return median_float(throughputs), median_float(times)

    def inject_fault(self, params: ReceiverResourceContentionParams):
        campus = self.scenario_name == "campus_lan"
        if campus:
            status = self._archive_job(params.host_name, "status")
            if status.get("pid"):
                raise RuntimeError("Archive job is already running on the receiver")
            self._archive_config = self.runtime.exec(
                params.host_name, "cat /etc/archive-job.json"
            )
        url = self._ensure_peer_large_object(params)
        bps, time_s = self._median_large_stats(params, url, max_time_sec=120)
        if bps is None or time_s is None:
            raise RuntimeError(
                "receiver_resource_contention baseline large HTTP measurement "
                f"failed on {params.host_name} -> {url}"
            )
        self._baseline_throughput_bps = bps
        self._baseline_time_s = time_s

        if campus:
            self._control_baseline_bps, _ = self._median_large_stats(
                params, url, max_time_sec=120, host=self._control_host(params)
            )
            if not self._control_baseline_bps:
                raise RuntimeError("Repository download failed on the control receiver")
            self.runtime.write_file(
                params.host_name,
                "/etc/archive-job.json",
                json.dumps({"workers": params.archive_workers}) + "\n",
            )
            self._archive_pid = self._archive_job(
                params.host_name, "start --duration 0"
            )["pid"]
            time.sleep(3)
            if not self.verify_fault(params)["verified"]:
                raise RuntimeError("Archive workers did not start on the receiver")
            return

        # Share one small CPU slice between the archive batch and the download.
        # A multi-core container lets the batch and curl run side by side,
        # so the transfer never slows down. Runtimes that reject quota updates
        # keep the workload-only path.
        self._archive = _stage_archive_job(
            self.runtime, params.host_name, params.stress_cpus
        )
        self._receiver_quota_applied = False
        try:
            self._original_nano_cpus = read_nano_cpus(self.runtime, params.host_name)
            set_nano_cpus(
                self.runtime,
                params.host_name,
                cpu_quota_to_nano_cpus(_RECEIVER_CPU_QUOTA),
            )
            self._receiver_quota_applied = True
        except Exception:  # noqa: BLE001
            self._receiver_quota_applied = False
        _launch_archive_job(self.runtime, params.host_name)
        deadline = time.time() + 30.0
        while time.time() < deadline:
            if _archive_job_running(container_commands(self.runtime, params.host_name)):
                break
            time.sleep(0.5)
        time.sleep(5.0)
        system_logger.info(
            "Injected TCP receiver resource contention on %s: "
            "workers=%d baseline_bps=%.0f url=%s",
            params.host_name,
            params.stress_cpus,
            bps,
            url,
        )

    def verify_fault(self, params: ReceiverResourceContentionParams) -> dict:
        """Verify the archive batch is running on the receiver."""
        if self.scenario_name == "campus_lan":
            status = self._archive_job(params.host_name, "status")
            return build_verify_result(
                fault_type=self.root_cause_name,
                verified=bool(status.get("pid"))
                and status.get("active_workers", 0) > 0
                and status.get("workers") == params.archive_workers,
                details={"host": params.host_name, **status},
            )
        running = _archive_job_running(
            container_commands(self.runtime, params.host_name)
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=running,
            details={"host": params.host_name, "workload_running": running},
        )

    def oracle_playbook(
        self, params: ReceiverResourceContentionParams
    ) -> OraclePlaybook:
        receiver = params.host_name
        url = self._resolve_large_url(params)
        steps = [
            _download_probe(receiver, url, f"Download the large object on {receiver}."),
            *_cpu_contention_steps(receiver, quota_evidence=False),
        ]
        peers = self._resolve_peer_hosts(params)
        if peers:
            steps.append(
                shell(
                    peers[0],
                    "cat /sys/fs/cgroup/cpu.max 2>&1; uptime",
                    f"Check that the sending server {peers[0]} has CPU to spare.",
                )
            )
        return OraclePlaybook(
            finding=(
                f"The receiving host {receiver} is CPU-starved by CPU-bound "
                "batch workers, so it reads incoming data slowly and its "
                "downloads crawl while the network path is healthy."
            ),
            steps=steps,
        )

    def recover_fault(self, params: ReceiverResourceContentionParams) -> dict:
        if self.scenario_name == "campus_lan":
            if self._archive_pid is None:
                return {"verified": False, "details": {"error": "no_owned_archive_job"}}
            stopped = self._archive_job(
                params.host_name, f"stop --pid {self._archive_pid}"
            ).get("stopped", False)
            self.runtime.write_file(
                params.host_name,
                "/etc/archive-job.json",
                self._archive_config or '{"workers": 1}\n',
            )
            bps, time_s = self._median_large_stats(
                params, self._resolve_large_url(params), max_time_sec=120
            )
            ratio = (
                bps / self._baseline_throughput_bps
                if (bps and self._baseline_throughput_bps)
                else None
            )
            return {
                "verified": bool(stopped and ratio is not None and ratio >= 0.70),
                "details": {
                    "archive_stopped": stopped,
                    "throughput_bps": bps,
                    "time_s": time_s,
                    "restore_ratio": ratio,
                    "downloads": self._last_downloads,
                },
            }
        quota_restored = True
        quota_error = None
        if getattr(self, "_receiver_quota_applied", False):
            try:
                set_nano_cpus(
                    self.runtime,
                    params.host_name,
                    int(getattr(self, "_original_nano_cpus", 0) or 0),
                )
                quota_restored = read_nano_cpus(self.runtime, params.host_name) == int(
                    getattr(self, "_original_nano_cpus", 0) or 0
                )
            except Exception as exc:  # noqa: BLE001
                quota_restored = False
                quota_error = str(exc)
        _remove_archive_job(self.runtime, params.host_name, self._archive or {})
        time.sleep(1.0)
        gone = not _archive_job_running(
            container_commands(self.runtime, params.host_name)
        )
        return {
            "verified": gone and quota_restored,
            "details": {
                "host": params.host_name,
                "workload_gone": gone,
                "quota_restored": quota_restored,
                "quota_error": quota_error,
            },
        }
