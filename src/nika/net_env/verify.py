"""Shared helpers for post-deploy net_env verification."""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nika.net_env.base import NetworkEnvBase
    from nika.runtime.base import LabRuntime


def _lab_ready_defaults() -> tuple[float, float]:
    """Return the configured startup-verification window and retry delay."""
    try:
        from nika.run_config.loader import get_run_config

        lab = get_run_config().nika.lab
        return float(lab.ready_max_wait_sec), float(lab.ready_retry_delay_sec)
    except Exception:  # noqa: BLE001
        return 180.0, 5.0


def build_lab_verify_result(
    *,
    scenario_name: str,
    verified: bool,
    checks: dict[str, bool],
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "verified": verified,
        "scenario_name": scenario_name,
        "checks": dict(checks),
        "details": details or {},
    }


def exec_or_empty(
    runtime: "LabRuntime", host: str, command: str, timeout: float = 10.0
) -> str:
    try:
        return runtime.exec(host, command, timeout=timeout)
    except Exception:
        return ""


def nodes_deployed(runtime: "LabRuntime", expected: Iterable[str]) -> bool:
    return set(expected).issubset(set(runtime.list_nodes()))


def ping_ok(runtime: "LabRuntime", host: str, target: str, *, count: int = 1) -> bool:
    output = exec_or_empty(runtime, host, f"ping -c {count} -W 2 {target}", timeout=15)
    return f"{count} received" in output or f"{count} packets received" in output


def link_up(runtime: "LabRuntime", host: str, intf: str = "eth0") -> bool:
    return (
        exec_or_empty(runtime, host, f"cat /sys/class/net/{intf}/operstate").strip()
        == "up"
    )


def host_has_ipv4(
    runtime: "LabRuntime", host: str, address: str, intf: str = "eth0"
) -> bool:
    output = exec_or_empty(runtime, host, f"ip -4 -o addr show dev {intf}")
    return address in output


def default_route_via(runtime: "LabRuntime", host: str, gateway: str) -> bool:
    return f"via {gateway}" in exec_or_empty(runtime, host, "ip route show default")


def process_running(runtime: "LabRuntime", host: str, process: str) -> bool:
    return bool(exec_or_empty(runtime, host, f"pgrep -x {process}").strip())


def service_active(runtime: "LabRuntime", host: str, unit: str) -> bool:
    return (
        exec_or_empty(runtime, host, f"systemctl is-active {unit}").strip() == "active"
    )


def http_ok(runtime: "LabRuntime", host: str, url: str) -> bool:
    output = exec_or_empty(
        runtime,
        host,
        f"curl -s -o /dev/null -w '%{{http_code}}' --connect-timeout 5 {url}",
        timeout=20,
    )
    return output.strip() == "200"


def frr_bgp_established(
    runtime: "LabRuntime", router: str, *, min_neighbors: int = 1
) -> bool:
    output = exec_or_empty(runtime, router, "vtysh -c 'show bgp summary'", timeout=20)
    established = 0
    for line in output.splitlines():
        fields = line.split()
        if fields and fields[-1].isdigit():
            established += 1
    return established >= min_neighbors


def k8s_ready_node_count(output: str) -> int:
    ready = 0
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "Ready":
            ready += 1
    return ready


def verify_lab_with_retry(net_env: NetworkEnvBase) -> dict[str, Any] | None:
    """Poll ``net_env.verify_lab()`` until success or timeout.

    Returns ``None`` when the scenario defines no startup verification.
    """
    verify = getattr(net_env, "startup_verify_lab", net_env.verify_lab)
    result = verify()
    if result is None:
        return None

    default_wait, default_delay = _lab_ready_defaults()
    max_wait_sec = getattr(net_env, "VERIFY_MAX_WAIT_SEC", default_wait)
    retry_delay_sec = getattr(net_env, "VERIFY_RETRY_DELAY_SEC", default_delay)
    deadline = time.time() + max_wait_sec
    last_result = result
    while time.time() < deadline:
        last_result = verify()
        if last_result.get("verified", False):
            return last_result
        time.sleep(retry_delay_sec)

    failed_checks = {
        name: ok for name, ok in (last_result.get("checks") or {}).items() if not ok
    }
    raise RuntimeError(
        f"Lab verification failed for {net_env.name!r} "
        f"within {max_wait_sec}s; failed checks: {failed_checks or last_result}"
    )
