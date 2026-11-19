from mcp.server.fastmcp import FastMCP

from nika.mcp.session_context import get_lab_api, get_lab_runtime
from nika.utils.errors import safe_tool

mcp = FastMCP(
    name="kathara_base_mcp_server", host="127.0.0.1", port=8000, log_level="INFO"
)


@safe_tool
@mcp.tool()
def curl_web_test(host_name: str, url: str, times: int = 5) -> str:
    """Curl a URL repeatedly and return timing statistics (lookup, connect, TTFB, total)."""
    return get_lab_api().curl_web_test(host_name=host_name, url=url, times=times)


@safe_tool
@mcp.tool()
def iperf_test(
    client_host_name: str,
    server_host_name: str,
    duration: int = 10,
    client_args: str = "",
    server_args: str = "",
) -> str:
    """Run an iperf test between two lab hosts."""
    return get_lab_api().iperf_test(
        client_host_name=client_host_name,
        server_host_name=server_host_name,
        duration=duration,
        client_args=client_args,
        server_args=server_args,
    )


@safe_tool
@mcp.tool()
def active_tcp_probe(
    source: str,
    destination: str,
    source_port: int,
    destination_port: int,
    payload_seed: int,
    payload_size: int = 256,
    packets: int = 32,
    destination_address: str | None = None,
) -> dict:
    """Send deterministic TCP payload probes over a chosen 5-tuple.

    ``source`` and ``destination`` are lab node names; the listener runs on
    ``destination``. Set ``destination_address`` to connect to another IP the
    destination node owns (for example its loopback) instead of its data-plane
    address. Change endpoints or ports to exercise a
    different ECMP path. Reports endpoint observations only; does not infer
    intermediate nodes.
    """
    from nika.traffic.active_probe import run_active_tcp_probe

    return run_active_tcp_probe(
        get_lab_runtime(),
        source=source,
        destination=destination,
        source_port=source_port,
        destination_port=destination_port,
        payload_seed=payload_seed,
        payload_size=payload_size,
        packets=packets,
        destination_address=destination_address,
    )


@safe_tool
@mcp.tool()
def exec_shell(host_name: str, command: str, timeout: float = 10) -> str:
    """Execute a shell command on a host, server or controller (seconds timeout)."""
    from nika.mcp.gateway.access import node_roles_for_session, SHELL_NODE_ROLES
    from nika.mcp.session_context import require_session_id

    if (
        node_roles_for_session(require_session_id()).get(host_name)
        not in SHELL_NODE_ROLES
    ):
        raise ValueError("Shell access is not allowed on this node role")
    return get_lab_api().exec_cmd(host_name, command, timeout=timeout)


@safe_tool
@mcp.tool()
def device_read(host_name: str, commands: list[str]) -> str:
    """Run router or switch CLI commands, including configuration commands.

    FRR devices support vtysh and Linux networking tools. OVS devices support
    ovs-ofctl and ovs-vsctl. Arguments are quoted at the device shell boundary.
    Supply one to eight commands. Each command has a ten-second timeout.
    Combined output is capped at 64 KiB. Long conntrack tables include exact
    reply-destination port ranges before the connection rows.
    """
    from nika.mcp.device_read import (
        MAX_OUTPUT,
        TIMEOUT_MARKER,
        bounded_command,
        conntrack_display,
        device_read_argv,
        require_device,
    )

    if not 1 <= len(commands) <= 8:
        raise ValueError("Supply one to eight device commands")
    parsed = [device_read_argv(command) for command in commands]
    for _, argv in parsed:
        require_device(host_name, argv)
    outputs = []
    remaining = MAX_OUTPUT
    for _, argv in parsed:
        output = get_lab_api().exec_cmd(host_name, bounded_command(argv), timeout=12)
        if TIMEOUT_MARKER in output or output.startswith("[TIMEOUT]"):
            raise TimeoutError("Device command exceeded its ten-second timeout")
        if len(output.encode()) > remaining:
            raise ValueError("Device output exceeds 64 KiB; narrow the command")
        if argv[:2] == ["conntrack", "-L"]:
            output = conntrack_display(output)
        outputs.append(output)
        remaining -= len(output.encode()) + 1
    return "\n".join(outputs)


@safe_tool
@mcp.tool()
def device_protocol_counters(host_name: str) -> str:
    """Read aggregate IPv4, ICMP, TCP, and UDP traffic and checksum counters on a Linux router."""
    from nika.mcp.device_read import require_device

    require_device(host_name, ["ip", "link", "show"])
    return get_lab_api().exec_cmd(host_name, "cat /proc/net/snmp", timeout=5)


@safe_tool
@mcp.tool()
def device_interface_state(host_name: str, interface: str) -> str:
    """Read the physical carrier state of one declared router or switch attachment."""
    import json
    from nika.mcp.session_context import get_session_meta
    from nika.problems.rca.inventory import (
        interfaces_for_node,
        load_session_offline_net_env,
    )
    from nika.runtime.spec import NodeRole

    env = load_session_offline_net_env(get_session_meta())
    identity = env.machine_identities.get(host_name)
    if identity is None or identity.role not in {NodeRole.ROUTER, NodeRole.SWITCH}:
        raise ValueError("Select a declared router or switch")
    if interface not in interfaces_for_node(env, host_name):
        raise ValueError("Select a declared data interface")
    state = get_lab_runtime().get_interface_operstate(host_name, interface)
    return json.dumps({"interface": interface, "operstate": state})


if __name__ == "__main__":
    mcp.run(transport="stdio")
