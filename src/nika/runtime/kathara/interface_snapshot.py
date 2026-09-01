"""Capture and restore Linux interface attachment state after VDE port moves."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nika.runtime.base import RuntimeCapabilityError

if TYPE_CHECKING:
    from nika.runtime.base import LabRuntime


@dataclass(frozen=True)
class LinkAttachmentState:
    """Observed L2/L3 state for one lab node interface."""

    node: str
    intf: str
    number: int
    mac: str
    mtu: int
    master: str | None
    addresses: tuple[str, ...]
    routes: tuple[dict, ...] = ()
    neighbors: tuple[dict, ...] = ()


def capture_link_state(
    runtime: LabRuntime, node: str, intf: str, number: int
) -> LinkAttachmentState:
    """Snapshot interface identity, master, addresses, and routes from the kernel."""
    info = _link_info(runtime, node, intf)
    master = info.get("master")
    return LinkAttachmentState(
        node=node,
        intf=intf,
        number=number,
        mac=info["address"],
        mtu=info["mtu"],
        master=str(master) if master else None,
        addresses=_addresses(runtime, node, intf),
        routes=_routes(runtime, node, intf),
        neighbors=_neighbors(runtime, node, intf),
    )


def restore_link_state(runtime: LabRuntime, state: LinkAttachmentState) -> None:
    """Rename the new NIC and restore master, addresses, routes, and operstate."""
    for _ in range(10):
        try:
            links = json.loads(runtime.exec(state.node, "ip -j link"))
        except json.JSONDecodeError:
            time.sleep(0.5)
            continue
        matching = next(
            (
                item
                for item in links
                if item.get("address", "").lower() == state.mac.lower()
            ),
            None,
        )
        if matching is None:
            time.sleep(0.5)
            continue
        current = matching["ifname"]
        if current != state.intf:
            runtime.exec(
                state.node,
                f"ip link set dev {current} name {state.intf}",
            )
        runtime.exec(state.node, f"ip link set dev {state.intf} mtu {state.mtu}")
        if state.master:
            runtime.exec(
                state.node,
                f"ip link set dev {state.intf} master {state.master}",
            )
        for address in state.addresses:
            runtime.exec(
                state.node,
                f"ip address replace {address} dev {state.intf}",
            )
        for row in state.neighbors:
            cmd = _neigh_replace_cmd(row, state.intf)
            if cmd:
                runtime.exec(state.node, cmd)
        runtime.exec(state.node, f"ip link set dev {state.intf} up")
        ordered_routes = sorted(
            state.routes,
            key=lambda row: 1 if row.get("dst") == "default" else 0,
        )
        for row in ordered_routes:
            runtime.exec(state.node, _route_replace_cmd(row, state.intf))
        return
    raise RuntimeCapabilityError(
        f"VDE proxy did not attach {state.node}:{state.intf}"
    )


def wait_for_bmv2_dataplane(
    runtime: LabRuntime, nodes: list[str], timeout_sec: float = 60.0
) -> None:
    """Wait until simple_switch_grpc is running on each named switch."""
    deadline = time.time() + timeout_sec
    pending = list(nodes)
    while pending and time.time() < deadline:
        still = [
            name
            for name in pending
            if not runtime.exec(name, "pgrep -x simple_switch_grpc", timeout=10).strip()
        ]
        pending = still
        if pending:
            time.sleep(2)
    if pending:
        raise RuntimeCapabilityError(
            f"simple_switch_grpc not ready on {pending}"
        )


def wait_for_bmv2_grpc_listen(
    runtime: LabRuntime, nodes: list[str], timeout_sec: float = 120.0
) -> None:
    """Wait until BMv2 gRPC is listening on port 9559 inside each switch."""
    deadline = time.time() + timeout_sec
    pending = list(nodes)
    while pending and time.time() < deadline:
        still = []
        for name in pending:
            listening = runtime.exec(
                name,
                "ss -ltn 2>/dev/null | grep -q ':9559' && echo up || echo down",
                timeout=10,
            ).strip()
            if listening != "up":
                still.append(name)
        pending = still
        if pending:
            time.sleep(2)
    if pending:
        raise RuntimeCapabilityError(f"BMv2 gRPC not listening on {pending}")


def restart_bmv2_dataplane(runtime: LabRuntime, node: str) -> None:
    """Restart BMv2 simple_switch_grpc from the running process or startup script."""
    running = runtime.exec(
        node,
        "pgrep -af '[s]imple_switch_grpc' | head -1",
        timeout=10,
    ).strip()
    if running:
        line = running.split(None, 1)[-1]
    else:
        startup_paths = f"/hostlab/{node}.startup {node}.startup"
        line = runtime.exec(
            node,
            f"grep -h -E 'simple_switch_grpc' {startup_paths} 2>/dev/null | head -1",
            timeout=15,
        ).strip()
    if not line:
        raise RuntimeCapabilityError(
            f"could not resolve simple_switch_grpc command for {node}"
        )
    line = line.rstrip()
    if line.endswith("&"):
        line = line[:-1].rstrip()
    if ">>" in line:
        line = line.split(">>", 1)[0].strip()

    runtime.exec(node, "pkill -f '[s]imple_switch_grpc' || true", timeout=10)
    time.sleep(1)
    runtime.exec(node, f"nohup {line} >> sw.log 2>&1 &", timeout=10)
    wait_for_bmv2_dataplane(runtime, [node])
    wait_for_bmv2_grpc_listen(runtime, [node])


def _neighbors(runtime: LabRuntime, node: str, intf: str) -> tuple[dict, ...]:
    try:
        rows = json.loads(runtime.exec(node, f"ip -j neigh show dev {intf}"))
    except json.JSONDecodeError:
        return ()
    kept: list[dict] = []
    for row in rows:
        states = row.get("state") or []
        if any(str(item).lower() == "permanent" for item in states):
            kept.append(row)
    return tuple(kept)


def _neigh_replace_cmd(row: dict, intf: str) -> str | None:
    dst = row.get("dst")
    lladdr = row.get("lladdr")
    if not dst or not lladdr:
        return None
    return f"ip neigh replace {dst} lladdr {lladdr} dev {intf} nud permanent"


def _routes(runtime: LabRuntime, node: str, intf: str) -> tuple[dict, ...]:
    """Capture non-kernel routes that would be dropped when the NIC is moved."""
    try:
        rows = json.loads(runtime.exec(node, "ip -j route show"))
    except json.JSONDecodeError:
        return ()
    kept: list[dict] = []
    for row in rows:
        if row.get("dev") != intf:
            continue
        if row.get("protocol") == "kernel" and row.get("scope") == "link":
            continue
        kept.append(row)
    return tuple(kept)


def _addresses(runtime: LabRuntime, node: str, intf: str) -> tuple[str, ...]:
    output = runtime.exec(node, f"ip -j address show dev {intf}")
    try:
        addresses = json.loads(output)[0].get("addr_info", [])
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeCapabilityError(
            f"could not inspect addresses for {node}:{intf}"
        ) from exc
    return tuple(
        f"{item['local']}/{item['prefixlen']}"
        for item in addresses
        if item.get("family") in {"inet", "inet6"} and item.get("local")
    )


def _link_info(runtime: LabRuntime, node: str, intf: str) -> dict:
    for _ in range(10):
        output = runtime.exec(node, f"ip -j link show dev {intf}")
        try:
            return json.loads(output)[0]
        except (IndexError, json.JSONDecodeError):
            time.sleep(0.5)
    raise RuntimeCapabilityError(f"could not inspect {node}:{intf}")


def _route_replace_cmd(row: dict, intf: str) -> str:
    dst = row.get("dst") or "default"
    parts = ["ip", "route", "replace", str(dst)]
    gateway = row.get("gateway")
    if gateway:
        parts.extend(["via", str(gateway)])
    parts.extend(["dev", intf])
    metric = row.get("metric")
    if metric is not None:
        parts.extend(["metric", str(metric)])
    prefsrc = row.get("prefsrc")
    if prefsrc:
        parts.extend(["src", str(prefsrc)])
    return " ".join(parts)
