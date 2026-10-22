"""P4 gateway fault adapters: P4Runtime writes and switch route geometry."""

from __future__ import annotations

import ipaddress
from nika.problems.support.p4runtime_writes import p4runtime_write, require_ok
from nika.runtime.base import LabRuntime


def read_table(runtime: LabRuntime, switch: str, table: str) -> list[dict] | None:
    """Read the live entries of one P4 table, or ``None`` when the read fails."""
    from nika.net_env.p4_dc_fabric.fabric_manager.apply import run_manager

    try:
        payload = run_manager(
            runtime, "read-table", "--switch", switch, "--table", table, timeout=30
        )
    except RuntimeError:
        return None
    rows = (payload.get("tables") or {}).get(table) if payload.get("ok") else None
    return rows if isinstance(rows, list) else None


def _modify_port_entry(
    runtime: LabRuntime,
    switch: str,
    table: str,
    field: str,
    port: int,
    action: str,
    params: dict[str, int],
) -> dict:
    spec = {
        "type": "MODIFY",
        "table": table,
        "match": {field: int(port)},
        "action": action,
        "params": params,
    }
    return require_ok(
        p4runtime_write(runtime, switch, [spec]), f"{table} update on {switch}"
    )


def set_ecn_threshold(
    runtime: LabRuntime, switch: str, port: int, threshold: int
) -> dict:
    return _modify_port_entry(
        runtime,
        switch,
        "ecn_config",
        "egress_port",
        port,
        "set_ecn_threshold",
        {"threshold": int(threshold)},
    )


def set_int_mtu(runtime: LabRuntime, switch: str, port: int, mtu: int) -> dict:
    return _modify_port_entry(
        runtime,
        switch,
        "int_mtu_config",
        "egress_spec",
        port,
        "set_int_mtu",
        {"mtu": int(mtu)},
    )


def set_icmp_frag_needed_filter(runtime: LabRuntime, switch: str) -> dict:
    spec = {
        "table": "icmp_acl",
        "match": {"type": 3, "code": 4},
        "action": "drop",
    }
    return require_ok(
        p4runtime_write(runtime, switch, [spec]), f"ICMP filter on {switch}"
    )


def ternary_miss_row(prefix: str, target_ip: str, others: list[str]) -> tuple[str, str]:
    """Value/mask of the addresses a flipped don't-care bit drops from *prefix*.

    A TCAM soft error that sets one mask bit of the host part (stored value 0)
    makes the row stop matching every address in *prefix* with that bit set.
    Choose the lowest such bit that the target has and the other addresses
    under the prefix lack, so the target loses its route and they keep it.
    """
    network = ipaddress.IPv4Network(prefix, strict=False)
    target = int(ipaddress.IPv4Address(target_ip))
    peers = [
        int(ipaddress.IPv4Address(ip))
        for ip in others
        if ipaddress.IPv4Address(ip) in network and ip != target_ip
    ]
    host_bits = 32 - network.prefixlen
    candidates = [b for b in range(host_bits) if target >> b & 1]
    if not candidates:
        raise ValueError(f"{target_ip} has no host bit set under {prefix}")
    bit = min(candidates, key=lambda b: (sum(1 for p in peers if p >> b & 1), b))
    base = int(network.network_address)
    mask = int(network.netmask) | (1 << bit)
    return (
        str(ipaddress.IPv4Address(base | (1 << bit))),
        str(ipaddress.IPv4Address(mask)),
    )
