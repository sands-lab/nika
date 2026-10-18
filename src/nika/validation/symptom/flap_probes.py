"""Periodic link-flap symptom probes (test-path only).

A fast echo train across the link shows the flap as alternating runs of lost
and answered echoes. A healthy link answers every echo; a dead link answers
none.
"""

from __future__ import annotations

import re
import time
from typing import Any

from nika.net_env.base import ProbePath
from nika.net_env.verify import exec_or_empty, ping_stats
from nika.problems.base import build_verify_result
from nika.runtime.base import LabRuntime
from nika.validation.symptom.probe import _resolve_path

BASELINE_PING_COUNT = 15
BASELINE_MAX_LOSS_PERCENT = 5.0
# 15 s of echoes every 100 ms spans several 1 s down / 1 s up cycles.
TRAIN_COUNT = 150
TRAIN_INTERVAL_SEC = 0.1
# A run shorter than this many echoes is jitter, not a link state.
MIN_RUN = 3
MIN_CYCLES = 3


def assert_baseline_healthy(
    runtime: LabRuntime,
    path: ProbePath,
) -> tuple[bool, dict[str, Any]]:
    """Require a healthy path before injecting a link fault."""
    if not path.dst_ip:
        return False, {"error": "no_probe_dst"}
    last_details: dict[str, Any] = {"error": "no_probe_attempt"}
    for attempt in range(3):
        stats = ping_stats(
            runtime,
            path.src_host,
            path.dst_ip,
            count=BASELINE_PING_COUNT,
            interval_sec=0.2,
        )
        last_details = {
            "src_host": path.src_host,
            "dst_ip": path.dst_ip,
            "loss_percent": stats.loss_percent,
            "received": stats.received,
            "transmitted": stats.transmitted,
            "attempt": attempt + 1,
        }
        if stats.received > 0 and stats.loss_percent <= BASELINE_MAX_LOSS_PERCENT:
            return True, last_details
        if attempt < 2:
            time.sleep(2.0)
    return False, last_details


def _echo_train(runtime: LabRuntime, src: str, dst: str) -> dict[str, Any]:
    """Answered (``!``) and lost (``.``) echoes in send order, and their runs."""
    output = exec_or_empty(
        runtime,
        src,
        f"ping -n -c {TRAIN_COUNT} -i {TRAIN_INTERVAL_SEC} -W 1 {dst}",
        timeout=TRAIN_COUNT * TRAIN_INTERVAL_SEC + 15.0,
    )
    # Only echo replies carry a TTL; "From ... icmp_seq=N Destination Unreachable" does not.
    answered = {int(seq) for seq in re.findall(r"icmp_seq=(\d+) ttl=", output)}
    pattern = "".join(
        "!" if seq in answered else "." for seq in range(1, TRAIN_COUNT + 1)
    )
    runs = [match.group(0) for match in re.finditer(r"!+|\.+", pattern)]
    return {
        "src": src,
        "dst": dst,
        "pattern": pattern,
        "loss_percent": round(100.0 * pattern.count(".") / TRAIN_COUNT, 1),
        "down_runs": sum(1 for run in runs if run[0] == "." and len(run) >= MIN_RUN),
        "up_runs": sum(1 for run in runs if run[0] == "!" and len(run) >= MIN_RUN),
    }


def flap_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Every echo of the train is answered on the healthy path."""
    topo_size = getattr(problem.net_env, "topo_size", None) or "s"
    path = _resolve_path(problem.scenario_name, params, topo_size=topo_size)
    if path is None or not path.dst_ip:
        return False, {"error": "no_probe_path"}
    train = _echo_train(problem.runtime, path.src_host, path.dst_ip)
    problem._audit_flap_before = train
    return train["loss_percent"] <= 1.0, train


def evaluate_link_flap_symptom(
    problem: Any,
    params: Any,
) -> tuple[bool, dict[str, Any]]:
    """Echo loss comes in repeated outages separated by recoveries."""
    before = getattr(problem, "_audit_flap_before", None)
    if not before:
        return False, {"error": "no_baseline_train"}
    train = _echo_train(problem.runtime, before["src"], before["dst"])
    periodic_loss = (
        20.0 <= train["loss_percent"] <= 80.0
        and train["down_runs"] >= MIN_CYCLES
        and train["up_runs"] >= MIN_CYCLES
    )
    return periodic_loss, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=periodic_loss,
        details={
            "before": before,
            "after": train,
            "comparison": {"periodic_loss": periodic_loss},
        },
    )
