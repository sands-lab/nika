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
    """Execute a shell command on a lab node, with an optional timeout in seconds."""
    return get_lab_api().exec_cmd(host_name, command, timeout=timeout)


if __name__ == "__main__":
    mcp.run(transport="stdio")
