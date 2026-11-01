"""Periodic link-flap symptom probes (test-path only).

A fast echo train across the link shows the flap as alternating runs of lost
and answered echoes. A healthy link answers every echo; a dead link answers
none.
"""

from __future__ import annotations

import re
import shlex
import time
from typing import Any

from nika.net_env.base import ProbePath
from nika.net_env.verify import exec_or_empty, ping_stats
from nika.problems.base import build_verify_result
from nika.runtime.base import LabRuntime
from nika.validation.symptom.path_probes import echo
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


def _echo_train(
    runtime: LabRuntime,
    src: str,
    dst: str,
    *,
    count: int = TRAIN_COUNT,
    separate_pings: bool = False,
) -> dict[str, Any]:
    """Answered (``!``) and lost (``.``) echoes in send order, and their runs."""
    deadline = round(count * TRAIN_INTERVAL_SEC) + 2
    if separate_pings:
        # A long-lived ping socket can stop after an interface reports a local
        # unreachable error. Reopen it for each sample while keeping one exec.
        command = (
            f"end=$(($(date +%s)+{deadline})); "
            'while [ "$(date +%s)" -lt "$end" ]; do '
            f"if ping -n -c 1 -W 0.2 {shlex.quote(dst)} >/dev/null 2>&1; "
            "then printf '!'; else printf '.'; fi; sleep 0.1; done"
        )
        output = exec_or_empty(runtime, src, command, timeout=deadline + 15.0)
        if not output or any(char not in "!." for char in output):
            return {"src": src, "dst": dst, "error": output[:300] or "no_ping_samples"}
        pattern = output
        sent = len(pattern)
    else:
        output = exec_or_empty(
            runtime,
            src,
            f"ping -n -c {count} -i {TRAIN_INTERVAL_SEC} -W 1 -w {deadline} {dst}",
            timeout=deadline + 15.0,
        )
        sent_match = re.search(r"(\d+) packets transmitted", output)
        if sent_match is None or output.startswith("[TIMEOUT]"):
            return {"src": src, "dst": dst, "error": output[:300] or "no_ping_summary"}
        sent = int(sent_match.group(1))
        # Only echo replies carry a TTL; ICMP unreachables do not.
        answered = {int(seq) for seq in re.findall(r"icmp_seq=(\d+) ttl=", output)}
        pattern = "".join("!" if seq in answered else "." for seq in range(1, sent + 1))
    runs = [match.group(0) for match in re.finditer(r"!+|\.+", pattern)]
    return {
        "src": src,
        "dst": dst,
        "sent": sent,
        "pattern": pattern,
        "loss_percent": round(100.0 * pattern.count(".") / sent, 1) if sent else 100.0,
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
    if "error" in train:
        return False, train
    baseline = echo(problem.runtime, path.src_host, path.dst_ip)
    train = {**train, "ttls": baseline["ttls"]}
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
    # Include four complete flap cycles regardless of probe start phase.
    count = max(
        TRAIN_COUNT,
        round(
            (MIN_CYCLES + 1) * (params.down_time + params.up_time) / TRAIN_INTERVAL_SEC
        ),
    )
    train = _echo_train(
        problem.runtime,
        before["src"],
        before["dst"],
        count=count,
        separate_pings=problem.lab_backend == "containerlab",
    )
    if "error" in train:
        return False, {"error": "flap_probe_failed", "after": train}
    # Loss tracks the configured down/up duty cycle. Failed-echo waits
    # under-count the down interval; NOS hold-down can stretch it.
    period = float(params.down_time + params.up_time)
    duty_percent = 100.0 * float(params.down_time) / period
    min_loss = max(1.0, 0.2 * duty_percent)
    max_loss = min(90.0, max(80.0, 4.0 * duty_percent))
    periodic_loss = (
        min_loss <= train["loss_percent"] <= max_loss
        and train["down_runs"] >= MIN_CYCLES
        and train["up_runs"] >= MIN_CYCLES
    )
    # A 1 s / 1 s flap on a meshed IGP link drops the adjacency and the
    # probe sticks to a longer detour, so later cycles are not lost.
    after_echo = echo(problem.runtime, before["src"], before["dst"])
    before_ttls = before.get("ttls") or []
    rerouted = (
        bool(before_ttls)
        and bool(after_echo["ttls"])
        and max(after_echo["ttls"]) < min(before_ttls)
        and train["down_runs"] >= 1
    )
    verified = periodic_loss or rerouted
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "before": before,
            "after": {**train, "ttls": after_echo["ttls"]},
            "comparison": {
                "periodic_loss": periodic_loss,
                "rerouted": rerouted,
            },
        },
    )
