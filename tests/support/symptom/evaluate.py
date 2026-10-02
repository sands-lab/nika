"""Unified test-path ``evaluate_symptom`` for every registered failure."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

from nika.net_env.verify import compare_symptom
from nika.problems.rca.inventory import resolve_default_intf
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
        path = _resolve_blackhole_path(runtime, params, path, problem)
    if failure == "host_incorrect_ip" and getattr(problem, "_original_ip", None):
        path = replace(path, old_ip=problem._original_ip)
    if failure == "mtu_mismatch" and problem is not None:
        path = _resolve_mtu_mismatch_path(problem, params, path)
    after = run_probe_snapshot(runtime, contract.probe, path, params=params)
    before_snap = before if before is not None else ProbeSnapshot()
    if after.extra.get("error") or before_snap.extra.get("error"):
        return False, {
            "error": after.extra.get("error") or before_snap.extra.get("error"),
            "before": before_snap.as_dict(),
            "after": after.as_dict(),
        }
    if contract.probe == "dns_answer":
        before_answers = before_snap.extra.get("dns_answers")
        after_answers = after.extra.get("dns_answers")
        ok = bool(before_answers and after_answers and before_answers != after_answers)
        return ok, {
            "failure": failure,
            "probe": contract.probe,
            "before": before_snap.as_dict(),
            "after": after.as_dict(),
            "comparison": {
                "expect": "client_dns_answer_changed",
                "observed": ok,
            },
        }
    if contract.probe == "bgp_hijack_route":
        was_absent = (
            before_snap.extra.get("bgp_query_ok") is True
            and before_snap.extra.get("bgp_target_present") is False
        )
        # Edge prefix filters can keep the hijack on the hijacking router; its
        # own hosts are then the ones whose traffic it captures.
        was_uncaptured = before_snap.extra.get("bgp_local_capture") is False
        deadline = time.monotonic() + _BGP_RIB_WITHDRAW_TIMEOUT_S
        while was_absent and time.monotonic() < deadline:
            if after.extra.get("bgp_target_present") is True or (
                was_uncaptured and after.extra.get("bgp_local_capture") is True
            ):
                break
            time.sleep(2.0)
            after = run_probe_snapshot(runtime, contract.probe, path, params=params)
        propagated = (
            after.extra.get("bgp_query_ok") is True
            and after.extra.get("bgp_target_present") is True
        )
        captured = was_uncaptured and after.extra.get("bgp_local_capture") is True
        ok = was_absent and (propagated or captured)
        return ok, {
            "failure": failure,
            "probe": contract.probe,
            "before": before_snap.as_dict(),
            "after": after.as_dict(),
            "comparison": {
                "expect": "prefix_reaches_remote_router_or_local_hosts",
                "propagated": propagated,
                "captured_locally": captured,
                "observed": ok,
            },
        }
    if failure in {"bgp_acl_block", "bgp_asn_misconfig"}:
        baseline = set(before_snap.extra.get("bgp_established_peers") or [])
        deadline = time.monotonic() + 35.0
        while time.monotonic() < deadline and after.extra.get("bgp_query_ok") is True:
            current = set(after.extra.get("bgp_established_peers") or [])
            if baseline and baseline - current:
                break
            time.sleep(2.0)
            after = run_probe_snapshot(runtime, contract.probe, path, params=params)
        current = set(after.extra.get("bgp_established_peers") or [])
        ok = bool(
            before_snap.extra.get("bgp_query_ok") is True
            and after.extra.get("bgp_query_ok") is True
            and baseline
            and baseline - current
        )
        return ok, {
            "failure": failure,
            "probe": contract.probe,
            "before": before_snap.as_dict(),
            "after": after.as_dict(),
            "comparison": {
                "expect": "established_peers_decline",
                "lost_peers": sorted(baseline - current),
                "observed": ok,
            },
        }
    if failure in {"ospf_acl_block", "ospf_area_misconfiguration"}:
        baseline_neighbors = before_snap.extra.get("ospf_full_neighbors")
        deadline = time.monotonic() + 50.0
        while time.monotonic() < deadline and after.extra.get("ospf_query_ok") is True:
            current_neighbors = after.extra.get("ospf_full_neighbors")
            expected_loss = (
                isinstance(baseline_neighbors, int)
                and isinstance(current_neighbors, int)
                and (
                    current_neighbors == 0
                    if failure == "ospf_acl_block"
                    else current_neighbors < baseline_neighbors
                )
            )
            if expected_loss:
                break
            time.sleep(2.0)
            after = run_probe_snapshot(runtime, contract.probe, path, params=params)
    if after.extra.get("error"):
        return False, {"error": after.extra["error"], "after": after.as_dict()}
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
    if failure == "ospf_area_misconfiguration":
        baseline_neighbors = before_snap.extra.get("ospf_full_neighbors")
        current_neighbors = after.extra.get("ospf_full_neighbors")
        ok = (
            isinstance(baseline_neighbors, int)
            and isinstance(current_neighbors, int)
            and baseline_neighbors > 0
            and current_neighbors < baseline_neighbors
        )
        cmp_details = {
            **cmp_details,
            "ospf_full_neighbors_before": baseline_neighbors,
            "ospf_full_neighbors_after": current_neighbors,
        }
    if failure == "link_down":
        intf = resolve_default_intf(getattr(params, "intf_name", "eth0"), runtime)
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
        intf = resolve_default_intf(getattr(params, "intf_name", "eth0"), runtime)
        host = getattr(params, "host_name", None)
        interface_gone = not runtime.interface_exists(host, intf) if host else False
        # With an alternate path the IGP reroutes instead of blackholing; the
        # echo reply then crosses more routers and arrives with a lower TTL.
        ttl_before = before_snap.extra.get("reply_ttl")
        ttl_after = after.extra.get("reply_ttl")
        rerouted = (
            isinstance(ttl_before, int)
            and isinstance(ttl_after, int)
            and ttl_after < ttl_before
        )
        ok = (ok or rerouted) and interface_gone
        cmp_details = {
            **cmp_details,
            "interface_exists": not interface_gone,
            "interface_gone": interface_gone,
            "reply_ttl_before": ttl_before,
            "reply_ttl_after": ttl_after,
            "rerouted": rerouted,
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
