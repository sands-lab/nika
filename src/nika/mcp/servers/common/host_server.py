import asyncio

from mcp.server.fastmcp import FastMCP

from nika.mcp.session_context import get_lab_api, get_session_meta
from nika.utils.errors import safe_tool

mcp = FastMCP(
    name="kathara_base_mcp_server", host="127.0.0.1", port=8000, log_level="INFO"
)


@safe_tool
@mcp.tool()
def ping_pair(host_a: str, host_b: str, count: int = 4, args: str = "") -> str:
    """Ping from one lab host to another.

    Args:
        host_a: Source host.
        host_b: Destination host.
        count: Ping packet count (default 4).
        args: Extra ping CLI arguments.
    """
    return get_lab_api().ping_pair(host_a=host_a, host_b=host_b, count=count, args=args)


@safe_tool
@mcp.tool()
def traceroute(host_name: str, dst_ip: str) -> str:
    """Run traceroute from a lab host to a destination IPv4 address."""
    return get_lab_api().traceroute(host_name, dst_ip)


@safe_tool
@mcp.tool()
def systemctl_ops(host_name: str, service_name: str, operation: str) -> str:
    """Run systemctl start/stop/restart/status for a service on a host."""
    return get_lab_api().systemctl_ops(
        host_name=host_name, service_name=service_name, operation=operation
    )


@safe_tool
@mcp.tool()
def get_host_net_config(host_name: str) -> dict:
    """Return ifconfig, ip addr, and ip route for a host."""
    return get_lab_api().get_host_net_config(host_name=host_name)


@safe_tool
@mcp.tool()
def get_tc_statistics(host_name: str, intf_name: str) -> str:
    """Return tc statistics for one interface on a host."""
    return get_lab_api().tc_show_statistics(host_name=host_name, intf_name=intf_name)


@safe_tool
@mcp.tool()
def netstat(host_name: str, args: str = "-tuln") -> str:
    """Run netstat on a host (default ``-tuln``)."""
    return get_lab_api().netstat(host_name=host_name, args=args)


@safe_tool
@mcp.tool()
def ip_addr_statistics(host_name: str) -> str:
    """Return IP address statistics for a host."""
    return get_lab_api().ip_addr_statistics(host_name=host_name)


@safe_tool
@mcp.tool()
def ethtool(host_name: str, interface: str, args: str) -> str:
    """Run ethtool on a host interface with the given arguments."""
    return get_lab_api().ethtool(host_name=host_name, interface=interface, args=args)


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
) -> dict:
    """Send deterministic TCP payload probes over a chosen 5-tuple.

    Change endpoints or ports to exercise a different ECMP path. Reports
    endpoint observations only; does not infer intermediate nodes.
    """
    from traffic.active_probe import run_active_tcp_probe

    from nika.runtime.factory import runtime_for_session

    return run_active_tcp_probe(
        runtime_for_session(get_session_meta()),
        source=source,
        destination=destination,
        source_port=source_port,
        destination_port=destination_port,
        payload_seed=payload_seed,
        payload_size=payload_size,
        packets=packets,
    )


@safe_tool
@mcp.tool()
def cat_file(host_name: str, file_path: str) -> str:
    """Show the contents of a file on a host."""
    return get_lab_api().exec_cmd(host_name=host_name, command=f"cat {file_path}")


@safe_tool
@mcp.tool()
def exec_shell(host_name: str, command: str) -> str:
    """Execute a shell command on a host."""
    return get_lab_api().exec_cmd(host_name, command)


@safe_tool
@mcp.tool()
async def exec_shell_dual(
    host1: str,
    cmd1: str,
    host2: str,
    cmd2: str,
) -> dict[str, list[str]]:
    """Execute shell commands on two hosts concurrently."""
    lab_api = get_lab_api()
    result1, result2 = await asyncio.gather(
        lab_api.exec_cmd_async(host1, cmd1),
        lab_api.exec_cmd_async(host2, cmd2),
    )
    return {"host1": [result1], "host2": [result2]}


if __name__ == "__main__":
    mcp.run(transport="stdio")
