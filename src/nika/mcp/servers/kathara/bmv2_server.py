"""MCP tools for live BMv2 / P4Runtime access."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from nika.mcp.session_context import get_lab_name
from nika.service.kathara import KatharaBMv2API

mcp = FastMCP("kathara_bmv2_mcp_server")


@mcp.tool()
def p4rt_exec(args: str) -> str:
    """Read and write P4Runtime state through the fabric controller.

    Commands include probe, read, read-table, counters, apply, delete-lpm,
    insert, modify and delete. Use --help for manager arguments.
    Table writes accept --switch, --table, repeated --match and --param,
    --action, --priority, --default, --member-id and --group-id.
    """
    return KatharaBMv2API(lab_name=get_lab_name()).p4rt_exec(args)


@mcp.tool()
def lb_get_state(gateway: str, limit: int = 10, offset: int = 0) -> str:
    """Read LB connection occupancy, pending and active connections, pool version, and recent pool updates."""
    from nika.mcp.session_context import get_lab_runtime, get_session_meta
    from nika.problems.rca.inventory import load_session_offline_net_env

    env = load_session_offline_net_env(get_session_meta())
    if gateway not in getattr(getattr(env, "model", None), "gateways", ()):
        raise ValueError("Select a gateway with an LB management endpoint")
    import json

    if not 1 <= limit <= 20 or offset < 0:
        raise ValueError("Supply limit 1..20 and a nonnegative offset")
    state = json.loads(
        get_lab_runtime().exec(
            gateway, "curl -fsS --max-time 5 http://127.0.0.1:9081/v1/state", timeout=8
        )
    )
    for key in ("active_connections", "pending_connections", "update_events"):
        rows = state.get(key, [])
        if key == "update_events":
            rows = list(reversed(rows))
        state[key] = rows[offset : offset + limit]
        state[key + "_next_offset"] = (
            offset + limit if offset + limit < len(rows) else None
        )
    return json.dumps(state)


@mcp.tool()
def bmv2_get_ports(switch: str, interface: str | None = None) -> str:
    """Read BMv2 port bindings, carrier state, MTUs, and interface counters."""
    import json
    from nika.mcp.session_context import get_lab_runtime, get_session_meta
    from nika.problems.rca.inventory import load_session_offline_net_env

    env = load_session_offline_net_env(get_session_meta())
    if switch not in env.bmv2_switches:
        raise ValueError("Select a declared BMv2 switch")
    ports = env.model.ports[switch]
    if interface is not None:
        ports = [port for port in ports if port.name == interface]
        if not ports:
            raise ValueError("Select a declared data interface")
    from nika.service.lab.checked_exec import checked_exec

    output = checked_exec(
        get_lab_runtime().exec, switch, "ip -j -s link show", timeout=5
    )
    try:
        rows = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Invalid interface-state JSON from {switch}: {output[:200]!r}"
        ) from exc
    interfaces = {row["ifname"].split("@")[0]: row for row in rows}
    return json.dumps(
        {
            "switch": switch,
            "ports": [
                {
                    "port": port.bmv2_port,
                    "interface": port.name,
                    "state": {
                        key: interfaces.get(port.name, {}).get(key)
                        for key in ("operstate", "mtu", "flags", "stats64")
                    },
                }
                for port in ports
            ],
        }
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
