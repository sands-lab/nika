"""Private control adapter for P4 gateway benchmark fault hooks."""

from __future__ import annotations

import json
import shlex

from nika.runtime.base import LabRuntime


def _config(runtime: LabRuntime, switch: str, kind: str, **values: object) -> str:
    options = " ".join(
        f"--{name.replace('_', '-')} {shlex.quote(str(value))}"
        for name, value in values.items()
        if value is not None
    )
    output = runtime.exec(
        "fabric_mgr",
        f"python3 /opt/nika/p4rt_manager.py gateway-config --switch {shlex.quote(switch)} "
        f"--kind {shlex.quote(kind)} {options}",
        timeout=30,
    )
    payload = json.loads(output)
    if not payload.get("ok"):
        raise RuntimeError(f"P4Runtime gateway configuration failed: {payload}")
    return output


def set_silent_destination_drop(
    runtime: LabRuntime, switch: str, target_ip: str
) -> str:
    return _config(runtime, switch, "silent-drop", address=target_ip)


def set_deterministic_loss(
    runtime: LabRuntime, switch: str, port: int, threshold: int
) -> str:
    return _config(runtime, switch, "loss", port=port, value=threshold)


def set_ecn_threshold(
    runtime: LabRuntime, switch: str, port: int, threshold: int
) -> str:
    return _config(runtime, switch, "ecn", port=port, value=threshold)


def set_int_mtu(runtime: LabRuntime, switch: str, port: int, mtu: int) -> str:
    return _config(runtime, switch, "int-mtu", port=port, value=mtu)


def set_icmp_frag_needed_filter(runtime: LabRuntime, switch: str) -> str:
    return _config(runtime, switch, "icmp-frag-needed")


def _lb(
    runtime: LabRuntime, switch: str, kind: str, *, capacity: int | None = None
) -> dict:
    option = f" --capacity {capacity}" if capacity is not None else ""
    output = runtime.exec(
        "fabric_mgr",
        "python3 /opt/nika/p4rt_manager.py gateway-lb "
        f"--switch {shlex.quote(switch)} --kind {shlex.quote(kind)}{option}",
        timeout=60,
    )
    payload = json.loads(output)
    if not payload.get("ok"):
        raise RuntimeError(f"P4Runtime gateway L4 configuration failed: {payload}")
    return payload


def exhaust_lb_conn_table(runtime: LabRuntime, switch: str, capacity: int) -> dict:
    return _lb(runtime, switch, "exhaust", capacity=capacity)


def unsafe_lb_pool_update(runtime: LabRuntime, switch: str) -> dict:
    return _lb(runtime, switch, "unsafe-update")


def lb_state(runtime: LabRuntime, switch: str) -> dict:
    return _lb(runtime, switch, "state")
