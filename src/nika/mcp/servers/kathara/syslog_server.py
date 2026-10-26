from mcp.server.fastmcp import FastMCP

from nika.mcp.session_context import get_lab_api
from nika.service.kathara.syslog_api import syslog_query_for

mcp = FastMCP("kathara_syslog_mcp_server")


@mcp.tool()
def syslog_query(
    start_time: str,
    end_time: str | None = None,
    host: str | None = None,
    program: str | None = None,
    contains: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query syslog records exported by the lab devices.

    Each exporting device sends logs to the collector over the management
    network. ``host`` is that device name. ``program`` is the syslog tag
    (for example ``bgpd`` or ``zebra``). Records appear a few seconds after
    the device writes them.

    Args:
        start_time: Window start: ISO-8601, Unix seconds, or ``-N`` for N
            seconds ago (e.g. ``-600``).
        end_time: Window end in the same formats; defaults to now.
        host: Only records from this device.
        program: Only records from this syslog tag.
        contains: Substring that must appear in the message.
        limit: Maximum rows to return.
    """
    return syslog_query_for(
        get_lab_api(),
        start_time=start_time,
        end_time=end_time,
        host=host,
        program=program,
        contains=contains,
        limit=limit,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
