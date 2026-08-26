import asyncio
from typing import Dict, List

from mcp.server.fastmcp import FastMCP

from nika.service.mcp_server.session_context import get_lab_api
from nika.utils.errors import safe_tool

# Initialize FastMCP server
mcp = FastMCP(
    name="kathara_base_mcp_server", host="127.0.0.1", port=8000, log_level="INFO"
)


@safe_tool
@mcp.tool()
async def get_reachability() -> str:
    """Get the complete ping results from each host to all other hosts in the lab.
    Note that if there are too many hosts, only a subset of hosts will be tested to reduce time cost.

    Returns:
        str: The ping results from each host to all other hosts in the lab.
    """
    lab_api = get_lab_api()
    result = await lab_api.get_reachability()
    return result


@safe_tool
@mcp.tool()
def ping_pair(host_a: str, host_b: str, count: int = 4, args: str = "") -> str:
    """Ping from one host to another in the lab.

    Args:
        host_a (str): The name of the source host.
        host_b (str): The name of the destination host.
        count (int, optional): Number of ping packets to send. Defaults to 4.
        args (str, optional): Additional arguments for the ping command. Defaults to "".
    Returns:
        str: The ping result from host_a to host_b.
    """
    lab_api = get_lab_api()
    result = lab_api.ping_pair(host_a=host_a, host_b=host_b, count=count, args=args)
    return result


@safe_tool
@mcp.tool()
def traceroute(host_name: str, dst_ip: str) -> str:
    """Run traceroute from a lab host to a destination IP.

    Args:
        host_name (str): The name of the source host or router.
        dst_ip (str): Destination IPv4 address.
    Returns:
        str: Traceroute command output.
    """
    lab_api = get_lab_api()
    return lab_api.traceroute(host_name, dst_ip)


@safe_tool
@mcp.tool()
def systemctl_ops(host_name: str, service_name: str, operation: str) -> str:
    """Perform systemctl operations (start, stop, restart, status) on a host.

    Args:
        host_name (str): The name of the host.
        service_name (str): The name of the service.
        operation (str): The operation to perform (start, stop, restart, status).

    Returns:
        str: The output of the systemctl command.
    """
    lab_api = get_lab_api()
    result = lab_api.systemctl_ops(
        host_name=host_name, service_name=service_name, operation=operation
    )
    return result


@safe_tool
@mcp.tool()
def get_host_net_config(host_name: str) -> dict:
    """Get the network configuration of a host, including ifconfig, ip addr, and ip route.

    Args:
        host_name (str): The name of the host.

    Returns:
        dict: The network configuration of the host.
    """
    lab_api = get_lab_api()
    config = lab_api.get_host_net_config(host_name=host_name)
    return config


@safe_tool
@mcp.tool()
def get_tc_statistics(host_name: str, intf_name: str) -> str:
    """Get the traffic control (tc) statistics of a specific interface on a host.

    Args:
        host_name (str): The name of the host.
        intf_name (str): The name of the interface.

    Returns:
        str: The tc statistics of the specified interface.
    """
    lab_api = get_lab_api()
    stats = lab_api.tc_show_statistics(host_name=host_name, intf_name=intf_name)
    return stats


@safe_tool
@mcp.tool()
def netstat(host_name: str, args: str = "-tuln") -> str:
    """Run netstat command on a host with given arguments.

    Args:
        host_name (str): Name of the host.
        args (str, optional): Arguments for the netstat command. Defaults to "-tuln".

    Returns:
        str: The output of the netstat command.
    """
    lab_api = get_lab_api()
    result = lab_api.netstat(host_name=host_name, args=args)
    return result


@safe_tool
@mcp.tool()
def ip_addr_statistics(host_name: str) -> str:
    """Get IP address statistics of a host.

    Args:
        host_name (str): Name of the host.

    Returns:
        str: The IP address statistics of the host.
    """
    lab_api = get_lab_api()
    result = lab_api.ip_addr_statistics(host_name=host_name)
    return result


@safe_tool
@mcp.tool()
def ethtool(host_name: str, interface: str, args: str) -> str:
    """Run ethtool command on a host's interface with given arguments.

    Args:
        host_name (str): Name of the host.
        interface (str): Name of the interface.
        args (str): Arguments for the ethtool command.

    Returns:
        str: The output of the ethtool command.
    """
    lab_api = get_lab_api()
    result = lab_api.ethtool(host_name=host_name, interface=interface, args=args)
    return result


@safe_tool
@mcp.tool()
def curl_web_test(host_name: str, url: str, times: int = 5) -> str:
    """Perform curl test to a Web URL for several times and return timing statistics.

    Args:
        host_name (str): Name of the host.
        url (str): URL to curl.
        times (int, optional): Number of times to perform the curl test. Defaults to 5.

    Returns:
        str: The timing statistics of the curl command, including name lookup time, connect time, TTFB, and total time.
    """
    lab_api = get_lab_api()
    result = lab_api.curl_web_test(host_name=host_name, url=url, times=times)
    return result


@safe_tool
@mcp.tool()
def iperf_test(
    client_host_name: str,
    server_host_name: str,
    duration: int = 10,
    client_args: str = "",
    server_args: str = "",
) -> str:
    """Run an iperf test between two hosts in the lab.

    Args:
        client_host_name (str): Name of the client host.
        server_host_name (str): Name of the server host.
        duration (int, optional): Duration of the test in seconds. Defaults to 10.
        client_args (str, optional): Additional arguments for the client. Defaults to "".
        server_args (str, optional): Additional arguments for the server. Defaults to "".

    Returns:
        str: The output of the iperf test.
    """
    lab_api = get_lab_api()
    result = lab_api.iperf_test(
        client_host_name=client_host_name,
        server_host_name=server_host_name,
        duration=duration,
        client_args=client_args,
        server_args=server_args,
    )
    return result


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

    Change endpoints or ports to exercise a different ECMP path.  The result
    reports endpoint observations only and does not infer an intermediate node.
    """
    from traffic.active_probe import run_active_tcp_probe

    from nika.runtime.factory import runtime_for_session
    from nika.service.mcp_server.session_context import get_session_meta

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
    """Show contents of a file on a host.

    Args:
        host_name (str): Name of the host.
        file_path (str): Path to the file on the host.

    Returns:
        str: The contents of the file on the host.
    """
    lab_api = get_lab_api()
    result = lab_api.exec_cmd(host_name=host_name, command=f"cat {file_path}")
    return result


@safe_tool
@mcp.tool()
def exec_shell(
    host_name: str,
    command: str,
) -> str:
    """
    Execute a shell command on a host.

    Args:
        host_name (str): The name of the host.
        command (str): The shell command to execute.
    """

    lab_api = get_lab_api()
    result = lab_api.exec_cmd(host_name, command)
    return result


@safe_tool
@mcp.tool()
async def exec_shell_dual(
    host1: str,
    cmd1: str,
    host2: str,
    cmd2: str,
) -> Dict[str, List[str]]:
    """
    Execute shell commands on two hosts concurrently.
    Args:
        host1 (str): The name of the first host.
        cmd1 (str): The shell command to execute on the first host.
        host2 (str): The name of the second host.
        cmd2 (str): The shell command to execute on the second host.
    """
    lab_api = get_lab_api()

    result1, result2 = await asyncio.gather(
        lab_api.exec_cmd_async(host1, cmd1),
        lab_api.exec_cmd_async(host2, cmd2),
    )

    return {"host1": [result1], "host2": [result2]}


if __name__ == "__main__":
    mcp.run(transport="stdio")
