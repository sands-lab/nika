"""Unified test-path ``evaluate_symptom`` for every registered failure."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

from nika.net_env.verify import compare_symptom
from nika.problems.link_interface.link import _resolve_link_intf
from nika.runtime.kathara.runtime import KatharaRuntime
from tests.support.symptom.types import ProbeSnapshot
from nika.runtime.base import LabRuntime
from tests.support.symptom.contracts import get_symptom_contract
from tests.support.symptom.custom import evaluate_custom_symptom
from tests.support.symptom.probe import (
    _resolve_blackhole_path,
    _resolve_mtu_mismatch_path,
    _resolve_path,
    run_probe_snapshot,
    symptom_class_to_expect,
)

_BGP_RIB_WITHDRAW_TIMEOUT_S = 90.0


def _bgp_prefix_in_rib(runtime: LabRuntime, router: str, prefix: str) -> bool:
    out = runtime.exec(
        router,
        f"vtysh -c 'show bgp ipv4 unicast {prefix}' 2>/dev/null",
        timeout=30,
    )
    if "Network not in table" in out or "Unknown command" in out:
        return False
    network = prefix.split("/")[0]
    return network in out or prefix in out


def _wait_bgp_prefix_absent(
    runtime: LabRuntime, router: str, prefix: str, *, timeout_s: float
) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _bgp_prefix_in_rib(runtime, router, prefix):
            return True
        time.sleep(2.0)
    return not _bgp_prefix_in_rib(runtime, router, prefix)


def evaluate_symptom(
    runtime: LabRuntime,
    failure: str,
    params: Any,
    *,
    scenario: str | None,
    topo_size: str = "s",
    before: ProbeSnapshot | None = None,
    problem: Any | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Confirm expected network impact after inject (tests only).

    Uses the failure's symptom contract ``probe``. Failures with
    ``probe="custom"`` require a live ``problem`` instance and dispatch to
    ``tests.support.symptom.custom``.
    """
    contract = get_symptom_contract(failure)
    if contract.control_plane_only:
        return True, {
            "skipped": True,
            "reason": "control_plane_only",
            "symptom_class": contract.symptom_class,
        }
    if contract.probe == "artifact_only":
        return True, {
            "skipped": True,
            "reason": "artifact_only",
            "symptom_class": contract.symptom_class,
        }
    if contract.probe == "custom":
        if problem is None:
            return False, {
                "error": "custom_requires_problem_instance",
                "symptom_class": contract.symptom_class,
            }
        return evaluate_custom_symptom(failure, problem, params)

    path = _resolve_path(scenario, params, topo_size=topo_size)
    if path is None:
        return False, {"error": "no_probe_path", "scenario": scenario}
    if failure == "k8s_coredns_isolated" and problem is not None:
        targets = getattr(problem, "target_devices", None) or []
        if targets:
            path = replace(path, src_host=targets[0])
    if failure == "host_static_blackhole":
        path = _resolve_blackhole_path(runtime, params, path)
    if failure == "mtu_mismatch" and problem is not None:
        path = _resolve_mtu_mismatch_path(problem, params, path)
    after = run_probe_snapshot(runtime, contract.probe, path, params=params)
    before_snap = before if before is not None else ProbeSnapshot()
    expect = symptom_class_to_expect(contract.symptom_class)
    if contract.symptom_class == "gray":
        expect = "gray_loss"

    if contract.probe == "ping_old_ip":
        ok = after.ping_ok is False
        return ok, {
            "failure": failure,
            "probe": contract.probe,
            "symptom_class": contract.symptom_class,
            "before": before_snap.as_dict(),
            "after": after.as_dict(),
            "comparison": {
                "expect": "old_ip_unreachable",
                "observed": not after.ping_ok,
            },
        }
    if contract.probe == "route_get_onlink":
        ok = bool(after.extra.get("route_onlink"))
        return ok, {
            "failure": failure,
            "probe": contract.probe,
            "symptom_class": contract.symptom_class,
            "before": before_snap.as_dict(),
            "after": after.as_dict(),
            "comparison": {
                "expect": "route_onlink",
                "observed": after.extra.get("route_onlink"),
            },
        }
    if contract.probe == "iperf_throughput":
        bps = after.extra.get("bits_per_second")
        before_bps = before_snap.extra.get("bits_per_second")
        if before_bps is None:
            before_bps = before_snap.as_dict().get("bits_per_second")
        overlimits = after.extra.get("tbf_overlimits")
        ok = False
        if bps is not None:
            ok = float(bps) < 100_000.0 or (
                before_bps is not None and float(bps) < float(before_bps) * 0.5
            )
        # TBF overlimits are the direct artifact of link_capacity_bottleneck.
        if not ok and overlimits is not None and int(overlimits) > 0:
            ok = True
        expect_label = (
            "low_throughput"
            if failure == "link_capacity_bottleneck"
            else "low_throughput_or_tbf_overlimits"
        )
        return ok, {
            "failure": failure,
            "probe": contract.probe,
            "symptom_class": contract.symptom_class,
            "before": before_snap.as_dict(),
            "after": after.as_dict(),
            "comparison": {
                "expect": expect_label,
                "bps": bps,
                "before_bps": before_bps,
                "tbf_overlimits": overlimits,
            },
        }

    ok, cmp_details = compare_symptom(
        before_snap.as_dict(),
        after.as_dict(),
        expect,  # type: ignore[arg-type]
        loss_min_percent=contract.loss_min_percent,
        latency_factor=contract.latency_factor,
    )
    if failure == "link_down":
        backend = "kathara" if isinstance(runtime, KatharaRuntime) else "containerlab"
        intf = _resolve_link_intf(getattr(params, "intf_name", "eth0"), backend)
        host = getattr(params, "host_name", None)
        operstate = runtime.get_interface_operstate(host, intf) if host else "unknown"
        operstate_ok = operstate == "down"
        # Rich ISP topologies often keep an alternate path, so path_ping may
        # stay up while the injected interface is down. Operstate is the
        # authoritative link_down symptom.
        ok = operstate_ok
        cmp_details = {
            **cmp_details,
            "operstate": operstate,
            "operstate_down": operstate_ok,
            "path_unreachable": cmp_details.get("observed"),
        }
    if failure == "bgp_missing_route_advertisement" and not ok:
        # iBGP+IGP still reaches the originated loopback; RIB withdrawal is
        # the observable control-plane signal (dataplane stays up).
        prefix = getattr(params, "prefix", None)
        observer = getattr(params, "symptom_host", None)
        if prefix and observer:
            rib_absent = _wait_bgp_prefix_absent(
                runtime,
                observer,
                str(prefix),
                timeout_s=_BGP_RIB_WITHDRAW_TIMEOUT_S,
            )
            ok = rib_absent
            cmp_details = {
                **cmp_details,
                "bgp_rib_absent": rib_absent,
                "observer": observer,
                "prefix": prefix,
            }
    if failure == "link_detach":
        backend = "kathara" if isinstance(runtime, KatharaRuntime) else "containerlab"
        intf = _resolve_link_intf(getattr(params, "intf_name", "eth0"), backend)
        host = getattr(params, "host_name", None)
        interface_gone = not runtime.interface_exists(host, intf) if host else False
        ok = ok and interface_gone
        cmp_details = {
            **cmp_details,
            "interface_exists": not interface_gone,
            "interface_gone": interface_gone,
        }
    if not ok and contract.symptom_class in {"degradation", "latency"}:
        after_ms = after.http_time_ms
        before_ms = before_snap.http_time_ms
        if (
            before_ms is not None
            and before_ms < 500.0
            and after_ms is not None
            and after_ms >= 500.0
        ):
            ok = True
            cmp_details["absolute_latency_pass"] = after_ms
    return ok, {
        "failure": failure,
        "probe": contract.probe,
        "symptom_class": contract.symptom_class,
        "before": before_snap.as_dict(),
        "after": after.as_dict(),
        "comparison": cmp_details,
    }
