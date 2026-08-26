"""Software load-balancer overload via legitimate HTTP traffic surge."""

from __future__ import annotations

import shlex
import time
from typing import Any

from pydantic import BaseModel, Field

from nika.net_env.verify import http_download_stats, ping_stats
from nika.problems.base import (
    FailureDomain,
    ProblemBase,
    build_verify_result,
)
from nika.problems.rca import node_resource
from nika.problems.service_networking.ab_helpers import (
    AbSummary,
    ab_summary_to_dict,
    parse_ab_output,
)
from nika.problems.support.cpu_quota_helpers import (
    NANOCPUS_PER_CPU,
    clear_original_nano_cpus,
    cpu_quota_to_nano_cpus,
    load_original_nano_cpus,
    persist_original_nano_cpus,
    read_nano_cpus,
    set_nano_cpus,
)
from nika.utils.logger import system_logger

_WORKER_TAG = "nika_lb_ovld_worker_"
_VIP_PING_HOST = "web99.local"

# Behavioral gates (fixed workload; not adaptive at runtime).
_NGINX_CPU_MIN_RATIO = 0.80
_VIP_TAIL_MIN_RATIO = 2.0
# web0 shares the campus farm uplink with VIP flood traffic, so mild inflation
# is expected. Require success and that control stays clearly healthier than VIP.
_CONTROL_P95_BASELINE_MAX_RATIO = 1.5
_CONTROL_ABS_P95_MAX_MS = 100.0
_CONTROL_VS_VIP_MAX_RATIO = 0.5
_BACKEND_CPU_MAX_RATIO = 0.50
_BACKEND_LOCAL_URL = "http://127.0.0.1/small"
_MAX_LOSS_PERCENT = 5.0
_RTT_ABS_MAX_MS = 20.0
_RTT_MAX_RATIO = 5.0
_RECOVER_VIP_P95_MAX_RATIO = 2.0
_MIN_ERROR_COUNT_FOR_DEGRADATION = 3


class LoadBalancerOverloadParams(BaseModel):
    """Parameters for software LB capacity overload via HTTP surge."""

    host_name: str = Field(description="NGINX load balancer host name.")
    client_host: str = Field(
        description="Probe client host for VIP/control measurements."
    )
    vip_url: str = Field(
        default="http://web99.local/small",
        description="HTTP URL through the NGINX VIP (lightweight endpoint).",
    )
    control_url: str = Field(
        default="http://web0.local/small",
        description="Parallel farm web URL (not a VIP upstream).",
    )
    backend_url: str = Field(
        default="http://20.200.0.2/small",
        description="VIP upstream backend URL probed from the LB container.",
    )
    backend_probe_host: str = Field(
        default="load_balancer",
        description="Host that executes the direct-backend HTTP probe.",
    )
    backend_cpu_host: str = Field(
        default="backend_web_0",
        description="Backend host used for CPU saturation control checks.",
    )
    load_client_hosts: str = Field(
        default="pc_2_1_1_1",
        description="Comma-separated hosts that generate background VIP load.",
    )
    cpu_quota: float = Field(
        default=0.2,
        description="Fixed LB CPU capacity (fractional CPUs) for the whole experiment.",
    )
    concurrency: int = Field(
        default=160,
        description="ApacheBench concurrency per background load worker.",
    )
    load_workers: int = Field(
        default=2,
        description="Background ab worker processes per load client host.",
    )
    warmup_sec: float = Field(
        default=5.0,
        description="Seconds to wait after starting background load before probes.",
    )
    probe_requests: int = Field(
        default=60,
        description="Request count for independent ab probe runs.",
    )
    probe_concurrency: int = Field(
        default=4,
        description="Concurrency for independent ab probe runs.",
    )
    probe_timeout_sec: int = Field(
        default=60,
        description="Max wall time for a single ab probe invocation.",
    )
    cpu_sample_sec: float = Field(
        default=1.5,
        description="Interval for Docker container CPU sampling.",
    )
    duration_sec: int = Field(
        default=300,
        description="Reserved for future timed load; workers run until recover.",
    )


class LoadBalancerOverload(ProblemBase):
    failure_domain = FailureDomain.SERVICE_NETWORKING
    root_cause_name: str = "load_balancer_overload"
    TAGS: list[str] = ["load_balancer", "http"]
    COMPATIBLE_COLUMNS = frozenset({"campus_lan"})
    Params = LoadBalancerOverloadParams
    symptom_desc = (
        "Legitimate HTTP traffic exceeds the software load balancer's processing "
        "capacity. Requests through the VIP show elevated tail latency or limited "
        "errors while NGINX CPU saturates; direct backend and parallel web paths, "
        "and the underlying network, remain healthy."
    )

    def __init__(self, scenario_name: str | None = None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._original_nano_cpus: int | None = None
        self._pinned_nano_cpus: int | None = None
        self._baseline: dict[str, Any] | None = None
        self._load_hosts: list[str] = []

    def root_cause_resources(self, params: LoadBalancerOverloadParams):
        return [node_resource(params.host_name)]

    def _parse_load_hosts(self, params: LoadBalancerOverloadParams) -> list[str]:
        hosts = [
            h.strip() for h in (params.load_client_hosts or "").split(",") if h.strip()
        ]
        if not hosts:
            hosts = [params.client_host]
        return hosts

    def _ensure_ab(self, host: str) -> None:
        check = self.runtime.exec(
            host, "command -v ab >/dev/null 2>&1 && echo OK || echo MISSING", timeout=10
        ).strip()
        if "OK" in check:
            return
        install = self.runtime.exec(
            host,
            "export DEBIAN_FRONTEND=noninteractive; "
            "apt-get update -qq && "
            "apt-get install -y -qq apache2-utils >/tmp/nika_ab_install.log 2>&1; "
            "command -v ab >/dev/null 2>&1 && echo OK || echo FAIL",
            timeout=180,
        ).strip()
        if "OK" not in install:
            log = self.runtime.exec(
                host,
                "tail -n 40 /tmp/nika_ab_install.log 2>/dev/null || true",
                timeout=10,
            )
            raise RuntimeError(f"apachebench (ab) unavailable on {host}: {log!r}")

    def _run_ab(
        self,
        host: str,
        url: str,
        *,
        requests: int,
        concurrency: int,
        timeout_sec: int,
    ) -> AbSummary:
        quoted = shlex.quote(url)
        # No -k: short-lived HTTP/1.0 connections (Connection: close).
        cmd = (
            f"ab -n {int(requests)} -c {int(concurrency)} -s {int(timeout_sec)} "
            f"{quoted} 2>&1 || true"
        )
        raw = self.runtime.exec(host, cmd, timeout=timeout_sec + 30)
        return parse_ab_output(raw)

    def _sample_container_cpu_percent(
        self, host: str, *, sample_sec: float
    ) -> float | None:
        """Return container CPU usage as percent of one host CPU (Docker formula)."""
        try:
            container = self.runtime.get_container(host)
            s1 = container.stats(stream=False)
            time.sleep(max(0.2, sample_sec))
            s2 = container.stats(stream=False)
        except Exception:  # noqa: BLE001
            return None

        def _total(stats: dict) -> int:
            return int(
                (stats.get("cpu_stats") or {}).get("cpu_usage", {}).get("total_usage")
                or 0
            )

        def _system(stats: dict) -> int:
            return int((stats.get("cpu_stats") or {}).get("system_cpu_usage") or 0)

        def _online(stats: dict) -> int:
            cpu = stats.get("cpu_stats") or {}
            online = cpu.get("online_cpus")
            if online:
                return int(online)
            percpu = (cpu.get("cpu_usage") or {}).get("percpu_usage") or []
            return max(1, len(percpu))

        cpu_delta = _total(s2) - _total(s1)
        system_delta = _system(s2) - _system(s1)
        if cpu_delta <= 0 or system_delta <= 0:
            return 0.0
        return (cpu_delta / system_delta) * _online(s2) * 100.0

    def _cpu_ratio_of_quota(
        self, host: str, *, quota_cpus: float, sample_sec: float
    ) -> float | None:
        """Fraction of the container's CPU quota currently used (1.0 = saturated)."""
        if quota_cpus <= 0:
            return None
        pct = self._sample_container_cpu_percent(host, sample_sec=sample_sec)
        if pct is None:
            return None
        return (pct / 100.0) / float(quota_cpus)

    def _nginx_running(self, host: str) -> bool:
        out = self.runtime.exec(
            host, "pgrep -x nginx >/dev/null 2>&1 && echo OK || echo NO", timeout=10
        ).strip()
        return "OK" in out

    def _active_connections(self, host: str) -> int:
        out = self.runtime.exec(
            host,
            "ss -Htan state established '( sport = :80 )' 2>/dev/null | wc -l",
            timeout=10,
        ).strip()
        try:
            return int(out.splitlines()[-1])
        except (IndexError, ValueError):
            return 0

    def _worker_counts(self, load_hosts: list[str]) -> tuple[int, int]:
        workers = 0
        ab_procs = 0
        for host in load_hosts:
            w_out = self.runtime.exec(
                host,
                f"ps -eo args 2>/dev/null | grep -c '[{_WORKER_TAG[0]}]{_WORKER_TAG[1:]}' || true",
                timeout=10,
            ).strip()
            a_out = self.runtime.exec(
                host,
                "ps -eo comm 2>/dev/null | grep -c '^ab$' || true",
                timeout=10,
            ).strip()
            try:
                workers += int(w_out.splitlines()[-1])
            except (IndexError, ValueError):
                pass
            try:
                ab_procs += int(a_out.splitlines()[-1])
            except (IndexError, ValueError):
                pass
        return workers, ab_procs

    def _stop_background_load(self, load_hosts: list[str]) -> None:
        for host in load_hosts:
            self.runtime.exec(
                host,
                f"pkill -f '[{_WORKER_TAG[0]}]{_WORKER_TAG[1:]}' 2>/dev/null || true; "
                "pkill -x ab 2>/dev/null || true",
                timeout=15,
            )

    def _start_background_load(
        self, params: LoadBalancerOverloadParams, load_hosts: list[str]
    ) -> None:
        quoted_url = shlex.quote(params.vip_url)
        for host in load_hosts:
            self._ensure_ab(host)
            self._stop_background_load([host])
            cmds = []
            for worker in range(params.load_workers):
                inner = (
                    "while true; do "
                    f"ab -n 200000000 -c {int(params.concurrency)} {quoted_url}; "
                    "sleep 0.05; done"
                )
                cmds.append(
                    "nohup bash -c "
                    + shlex.quote(inner)
                    + f" {_WORKER_TAG}{worker} </dev/null "
                    + f">/tmp/nika_lb_ovld_{worker}.log 2>&1 &"
                )
            self.runtime.exec(
                host,
                "command -v ab >/dev/null 2>&1 || exit 127; " + " ".join(cmds),
                timeout=20,
            )

        expected_workers = len(load_hosts) * params.load_workers
        deadline = time.monotonic() + 15.0
        workers = ab_procs = 0
        while time.monotonic() < deadline:
            time.sleep(0.5)
            workers, ab_procs = self._worker_counts(load_hosts)
            if workers >= expected_workers and ab_procs >= 1:
                break
        if workers < expected_workers or ab_procs < 1:
            logs = []
            for host in load_hosts:
                logs.append(
                    self.runtime.exec(
                        host,
                        "tail -n 30 /tmp/nika_lb_ovld_0.log 2>/dev/null || true",
                        timeout=10,
                    )
                )
            raise RuntimeError(
                "load_balancer_overload background traffic did not become ready: "
                f"workers={workers}/{expected_workers} ab={ab_procs} logs={logs!r}"
            )

    def _collect_baseline(self, params: LoadBalancerOverloadParams) -> dict[str, Any]:
        self._ensure_ab(params.client_host)

        vip = self._run_ab(
            params.client_host,
            params.vip_url,
            requests=params.probe_requests,
            concurrency=params.probe_concurrency,
            timeout_sec=params.probe_timeout_sec,
        )
        control = self._run_ab(
            params.client_host,
            params.control_url,
            requests=params.probe_requests,
            concurrency=params.probe_concurrency,
            timeout_sec=params.probe_timeout_sec,
        )
        backend_from_lb = http_download_stats(
            self.runtime,
            params.backend_probe_host,
            params.backend_url,
            max_time_sec=min(30, params.probe_timeout_sec),
            connect_timeout_sec=5,
        )
        # Latency control must not run on the saturated LB; probe the backend
        # application locally so we isolate LB CPU from backend service time.
        backend_local = http_download_stats(
            self.runtime,
            params.backend_cpu_host,
            _BACKEND_LOCAL_URL,
            max_time_sec=min(30, params.probe_timeout_sec),
            connect_timeout_sec=5,
        )
        ping = ping_stats(
            self.runtime,
            params.client_host,
            _VIP_PING_HOST,
            count=10,
            interval_sec=0.2,
        )
        lb_cpu = self._cpu_ratio_of_quota(
            params.host_name,
            quota_cpus=params.cpu_quota,
            sample_sec=params.cpu_sample_sec,
        )
        backend_cpu = self._cpu_ratio_of_quota(
            params.backend_cpu_host,
            quota_cpus=0.5,
            sample_sec=params.cpu_sample_sec,
        )

        if (
            vip.p95_ms is None
            or vip.complete_requests is None
            or vip.complete_requests < 1
        ):
            raise RuntimeError(
                f"load_balancer_overload VIP baseline probe failed: {vip.raw[-800:]!r}"
            )
        if control.p95_ms is None:
            raise RuntimeError(
                f"load_balancer_overload control baseline probe failed: {control.raw[-800:]!r}"
            )
        if not backend_from_lb.ok:
            raise RuntimeError(
                "load_balancer_overload backend-from-LB baseline probe failed: "
                f"{backend_from_lb.raw!r}"
            )
        if not backend_local.ok:
            raise RuntimeError(
                "load_balancer_overload backend-local baseline probe failed: "
                f"{backend_local.raw!r}"
            )
        if lb_cpu is not None and lb_cpu >= _NGINX_CPU_MIN_RATIO:
            raise RuntimeError(
                f"load_balancer_overload baseline LB already saturated: cpu_ratio={lb_cpu}"
            )

        return {
            "vip": ab_summary_to_dict(vip),
            "control": ab_summary_to_dict(control),
            "backend_from_lb_ok": backend_from_lb.ok,
            "backend_from_lb_time_s": backend_from_lb.time_total_s,
            "backend_local_ok": backend_local.ok,
            "backend_local_time_s": backend_local.time_total_s,
            "ping_rtt_ms": ping.rtt_avg_ms,
            "ping_loss_percent": ping.loss_percent,
            "lb_cpu_ratio": lb_cpu,
            "backend_cpu_ratio": backend_cpu,
            "nginx_running": self._nginx_running(params.host_name),
            "active_connections": self._active_connections(params.host_name),
        }

    def inject_fault(self, params: LoadBalancerOverloadParams):
        load_hosts = self._parse_load_hosts(params)
        self._load_hosts = load_hosts

        if not self._nginx_running(params.host_name):
            raise RuntimeError(f"nginx is not running on {params.host_name}")

        original = read_nano_cpus(self.runtime, params.host_name)
        self._original_nano_cpus = original
        persist_original_nano_cpus(self.runtime, params.host_name, original)

        pinned = cpu_quota_to_nano_cpus(params.cpu_quota)
        set_nano_cpus(self.runtime, params.host_name, pinned)
        self._pinned_nano_cpus = pinned

        # Capacity pin must settle before baseline (quota is not the fault).
        time.sleep(0.5)
        self._baseline = self._collect_baseline(params)

        self._start_background_load(params, load_hosts)
        time.sleep(max(0.5, params.warmup_sec))

        system_logger.info(
            "Injected load_balancer_overload on %s: cpu_quota=%.2f nano=%d "
            "concurrency=%d workers=%d hosts=%s vip_baseline_p95=%s",
            params.host_name,
            params.cpu_quota,
            pinned,
            params.concurrency,
            params.load_workers,
            load_hosts,
            (self._baseline.get("vip") or {}).get("p95_ms"),
        )

    def verify_fault(self, params: LoadBalancerOverloadParams) -> dict:
        """Verify load generators and CPU pin are injected (artifact gate)."""
        load_hosts = self._load_hosts or self._parse_load_hosts(params)
        expected_workers = len(load_hosts) * params.load_workers
        workers, ab_procs = self._worker_counts(load_hosts)
        load_running = workers >= max(1, expected_workers // 2) and ab_procs >= 1

        nginx_running = self._nginx_running(params.host_name)
        current_nano = read_nano_cpus(self.runtime, params.host_name)
        expected_nano = self._pinned_nano_cpus or cpu_quota_to_nano_cpus(
            params.cpu_quota
        )
        capacity_ok = current_nano == expected_nano
        verified = bool(load_running and capacity_ok and nginx_running)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "client_host": params.client_host,
                "load_hosts": load_hosts,
                "load_running": load_running,
                "workers": workers,
                "ab_processes": ab_procs,
                "nano_cpus": current_nano,
                "expected_nano_cpus": expected_nano,
                "capacity_ok": capacity_ok,
                "nginx_running": nginx_running,
                "active_connections": self._active_connections(params.host_name),
            },
        )

    def recover_fault(self, params: LoadBalancerOverloadParams) -> dict:
        load_hosts = self._load_hosts or self._parse_load_hosts(params)
        self._stop_background_load(load_hosts)
        time.sleep(2.0)

        workers, ab_procs = self._worker_counts(load_hosts)
        load_gone = workers == 0 and ab_procs == 0

        original = self._original_nano_cpus
        if original is None:
            original = load_original_nano_cpus(self.runtime, params.host_name)
        quota_restored = True
        restored_nano = read_nano_cpus(self.runtime, params.host_name)
        if original is not None:
            set_nano_cpus(self.runtime, params.host_name, original)
            clear_original_nano_cpus(self.runtime, params.host_name)
            restored_nano = read_nano_cpus(self.runtime, params.host_name)
            quota_restored = restored_nano == original

        time.sleep(1.0)
        vip = self._run_ab(
            params.client_host,
            params.vip_url,
            requests=params.probe_requests,
            concurrency=params.probe_concurrency,
            timeout_sec=params.probe_timeout_sec,
        )
        control = self._run_ab(
            params.client_host,
            params.control_url,
            requests=max(20, params.probe_requests // 2),
            concurrency=params.probe_concurrency,
            timeout_sec=params.probe_timeout_sec,
        )
        recover_quota = params.cpu_quota
        if original and original > 0:
            recover_quota = original / NANOCPUS_PER_CPU
        lb_cpu = self._cpu_ratio_of_quota(
            params.host_name,
            quota_cpus=recover_quota,
            sample_sec=params.cpu_sample_sec,
        )

        base_vip = (self._baseline or {}).get("vip") or {}
        base_p95 = base_vip.get("p95_ms")
        p95_ratio = (
            (vip.p95_ms / base_p95) if vip.p95_ms is not None and base_p95 else None
        )
        vip_recovered = (
            vip.p95_ms is not None
            and vip.error_count == 0
            and (p95_ratio is None or p95_ratio <= _RECOVER_VIP_P95_MAX_RATIO)
        )
        cpu_recovered = lb_cpu is None or lb_cpu < _NGINX_CPU_MIN_RATIO
        nginx_running = self._nginx_running(params.host_name)
        control_ok = control.p95_ms is not None and control.error_count == 0

        ok = bool(
            load_gone
            and quota_restored
            and vip_recovered
            and cpu_recovered
            and nginx_running
            and control_ok
        )
        system_logger.info(
            "recover_fault load_balancer_overload on %s: ok=%s p95_ratio=%s "
            "lb_cpu=%s load_gone=%s",
            params.host_name,
            ok,
            p95_ratio,
            lb_cpu,
            load_gone,
        )
        return {
            "verified": ok,
            "details": {
                "host": params.host_name,
                "load_gone": load_gone,
                "workers": workers,
                "ab_processes": ab_procs,
                "restored_nano_cpus": restored_nano,
                "expected_nano_cpus": original,
                "quota_restored": quota_restored,
                "nginx_running": nginx_running,
                "vip": ab_summary_to_dict(vip),
                "control": ab_summary_to_dict(control),
                "baseline_vip_p95_ms": base_p95,
                "p95_ratio": p95_ratio,
                "vip_recovered": vip_recovered,
                "lb_cpu_ratio": lb_cpu,
                "cpu_recovered": cpu_recovered,
                "control_ok": control_ok,
            },
        }
