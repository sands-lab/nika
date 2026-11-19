from __future__ import annotations

import json
import re
import time
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
from nika.problems.support.benchmark_targets import first
from nika.problems.support.recovery import FileSnapshot
from nika.problems.support.oracle import (
    Check,
    OraclePlaybook,
    OracleStep,
    ping,
    shell,
)
from nika.problems.support.cpu_quota_helpers import (
    container_commands,
    container_tcp_listeners,
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
    lines = output.splitlines()
    if not lines or lines[0] != "archive_children":
        return 0
    return sum(line.strip() == "gzip" for line in lines[1:])


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


def _cpu_contention_steps(host: str, *, quota_evidence: bool) -> list[OracleStep]:
    """Read the archive batch's children and CFS throttling on ``host``."""
    return [
        shell(
            host,
            "pid=; read -r pid 2>/dev/null < /run/archive-job.pid || :; "
            "printf 'archive_children\\n'; "
            'case "$pid" in ""|*[!0-9]*) :;; *) '
            "children=; read -r children 2>/dev/null "
            '< "/proc/$pid/task/$pid/children" || :; '
            "for child in $children; do "
            'comm=; read -r comm 2>/dev/null < "/proc/$child/comm" || :; '
            'if [ -n "$comm" ]; then printf "%s\\n" "$comm"; fi; '
            "done;; esac",
            f"Count running gzip children of the archive batch on {host}.",
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
            "for file in /sys/fs/cgroup/cpu.max /sys/fs/cgroup/cpu.stat; do "
            'if [ ! -r "$file" ]; then '
            'printf "Cannot read %s\\n" "$file" >&2; exit 1; fi; '
            'while IFS= read -r line || [ -n "$line" ]; do '
            'printf "%s\\n" "$line"; done < "$file"; done',
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


class EndpointCPUContentionParams(BaseModel):
    """Parameters for injecting endpoint CPU resource contention."""

    host_name: str = Field(description="Endpoint whose CPU is contended.")
    duration: int = Field(
        default=3600,
        description="Legacy duration parameter; contention persists until recovery.",
    )
    server_host: str | None = Field(
        default=None,
        description="HTTP server when the contended endpoint is the client.",
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


class EndpointCPUContention(ProblemBase):
    failure_domain = FailureDomain.ENDPOINT_APPLICATION
    symptom_probe = "custom"
    symptom_class = "degradation"
    dynamic_artifact = True
    root_cause_name: str = "endpoint_cpu_contention"
    CONFUSABLE_WITH = ("tcp_receive_window_limited",)
    # A 1 Mbit/s path can remain the bottleneck under the CPU quota.
    # Require a 10 Mbit/s path budget to expose the fivefold transfer slowdown.
    minimum_path_rate_kbps = 10_000
    description = (
        "An endpoint is CPU-starved by competing batch workers, slowing its transfers."
    )
    TAGS: list[str] = ["http"]
    Params = EndpointCPUContentionParams
    symptom_desc = (
        "An endpoint has insufficient CPU capacity under competing workload. "
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

    @classmethod
    def benchmark_inject_options(cls, ctx, base):
        from nika.net_env.net_env_pool import scenario_path_rate_limit_kbps

        cap = scenario_path_rate_limit_kbps(
            ctx.scenario, (ctx.isp_options or {}).get("device_profile")
        )
        if not base.get("client_host") or (cap is not None and cap < 10_000):
            return [base]
        return [
            base,
            {
                **base,
                "host_name": base["client_host"],
                "server_host": base["host_name"],
            },
        ]

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        model = getattr(ctx.net_env, "model", None)
        if model is None:
            return
        endpoints = {
            endpoint.name
            for endpoint in [*model.web_endpoints(), *model.client_endpoints()]
        }
        if inject.get("host_name") not in endpoints:
            raise ValueError(
                f"Unknown CPU contention endpoint: {inject.get('host_name')!r}"
            )

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._original_nano_cpus: int | None = None
        self._injected_nano_cpus: int | None = None
        self._baseline_throughput_bps: float | None = None
        self._baseline_time_s: float | None = None
        self._baseline_rtt_ms: float | None = None
        self._baseline_loss_percent: float | None = None
        self._archive: dict[str, Any] | None = None

    def root_cause_resources(self, params: EndpointCPUContentionParams):
        return [node_resource(params.host_name)]

    def parse_params(
        self, params: BaseModel | dict[str, Any] | None = None, **overrides: Any
    ) -> EndpointCPUContentionParams:
        parsed = super().parse_params(params, **overrides)
        assert isinstance(parsed, EndpointCPUContentionParams)
        return self._with_scenario_endpoints(parsed)

    def _with_scenario_endpoints(
        self, params: EndpointCPUContentionParams
    ) -> EndpointCPUContentionParams:
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
        endpoints = {endpoint.name for endpoint in [*webs, *clients]}
        if params.host_name not in endpoints:
            raise ValueError(f"Unknown CPU contention endpoint: {params.host_name!r}")
        web = next(
            (w for w in webs if w.name == (params.server_host or params.host_name)),
            webs[0],
        )
        client = next(
            (c for c in clients if getattr(c, "leaf_id", None) != web.leaf_id),
            clients[0] if clients else None,
        )
        defaults: dict[str, object] = {
            "dst_ip": web.ip,
            "small_url": f"http://{web.ip}/small.bin",
            "large_url": f"http://{web.ip}/large.bin",
        }
        if params.host_name in {c.name for c in clients}:
            defaults["client_host"] = params.host_name
            defaults["server_host"] = web.name
        elif client is not None:
            defaults["client_host"] = client.name
        updates = {
            key: value
            for key, value in defaults.items()
            if key not in params.model_fields_set
        }
        if not updates:
            return params
        return params.model_copy(update=updates)

    def _median_large_stats(
        self,
        params: EndpointCPUContentionParams,
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
        self, params: EndpointCPUContentionParams
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

    def inject_fault(self, params: EndpointCPUContentionParams):
        self._endpoint_ip = self.runtime.get_host_ip(
            params.host_name, with_prefix=False
        )
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
                "endpoint_cpu_contention baseline large HTTP measurement failed "
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
            "Injected endpoint_cpu_contention on %s: "
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

    def verify_fault(self, params: EndpointCPUContentionParams) -> dict:
        """Verify the archive batch and CPU quota are injected (artifact gate)."""
        commands = container_commands(self.runtime, params.host_name)
        stress_running = _archive_job_running(commands)
        port = urlsplit(params.large_url).port or 80
        listeners = container_tcp_listeners(
            self.runtime,
            params.server_host or params.host_name,
            port,
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

    def oracle_playbook(self, params: EndpointCPUContentionParams) -> OraclePlaybook:
        return OraclePlaybook(
            finding=f"CPU-bound batch workers on {params.host_name} contend with the endpoint's transfer workload.",
            steps=[
                ping(
                    params.client_host,
                    params.dst_ip,
                    "Check the path to the serving endpoint.",
                    count=5,
                ),
                _download_probe(
                    params.client_host, params.large_url, "Measure the bulk transfer."
                ),
                *_cpu_contention_steps(params.host_name, quota_evidence=True),
            ],
        )

    def recover_fault(self, params: EndpointCPUContentionParams) -> dict:
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
            "recover_fault endpoint_cpu_contention on %s: ok=%s "
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
