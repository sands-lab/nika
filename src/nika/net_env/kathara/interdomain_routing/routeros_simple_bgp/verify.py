"""Startup verification signals for the routeros_simple_bgp scenario."""

from __future__ import annotations

from typing import Any

from nika.net_env.kathara.interdomain_routing.routeros_simple_bgp.lab import (
    LINK_IFACE,
    MGMT_ADDR,
    MGMT_PASSWORD,
    MGMT_USER,
)
from nika.net_env.verify import (
    build_lab_verify_result,
    default_route_via,
    exec_or_empty,
    host_has_ipv4,
    nodes_deployed,
    ping_ok,
)
from nika.runtime.base import LabRuntime

# RouterOS's mgmt IP lives on vrnetlab's internal bridge, reachable only from
# inside the container's network namespace. Commands stay free of embedded
# double quotes: nika's exec wrapping (ShellResolver.wrap_shell_command)
# mis-escapes them, the same issue IOSXRAPIMixin works around with single
# quotes only.
CLI_COMMAND = (
    f"sshpass -p {MGMT_PASSWORD} ssh -q -oStrictHostKeyChecking=no "
    f"-oConnectTimeout=1 {MGMT_USER}@{MGMT_ADDR} '{{command}}'"
)


def _routeros_interface_up(runtime: LabRuntime, router: str, interface: str) -> bool:
    # `print terse` renders state as a single-letter flag (e.g. "R"), not as
    # a "running=..." field, so it can't be substring-matched; filtering
    # server-side on the (queryable but unlisted) `running` property instead
    # returns the row only when it is actually up.
    output = exec_or_empty(
        runtime,
        router,
        CLI_COMMAND.format(
            command=f"/interface print terse where name={interface} running=yes"
        ),
    )
    return bool(output.strip())


def _routeros_bgp_established(
    runtime: LabRuntime, router: str, *, min_neighbors: int = 1
) -> bool:
    # Same story as `_routeros_interface_up`: state is the "E" flag, not a
    # "state=established" field, so filter server-side on `established`.
    output = exec_or_empty(
        runtime,
        router,
        CLI_COMMAND.format(
            command="/routing/bgp/session/print terse where established=yes"
        ),
        timeout=20,
    )
    established = len([line for line in output.splitlines() if line.strip()])
    return established >= min_neighbors


def verify_routeros_simple_bgp_lab(
    runtime: LabRuntime, *, scenario_name: str
) -> dict[str, Any]:
    expected = ("router1", "router2", "pc1", "pc2")
    checks = {
        "nodes_deployed": nodes_deployed(runtime, expected),
        "router1_iface_up": _routeros_interface_up(runtime, "router1", LINK_IFACE),
        "router2_iface_up": _routeros_interface_up(runtime, "router2", LINK_IFACE),
        "router1_bgp_established": _routeros_bgp_established(runtime, "router1"),
        "pc1_ipv4": host_has_ipv4(runtime, "pc1", "195.11.14.2"),
        "pc2_ipv4": host_has_ipv4(runtime, "pc2", "200.1.1.2"),
        "pc1_default_route": default_route_via(runtime, "pc1", "195.11.14.1"),
        "pc1_gateway_reachable": ping_ok(runtime, "pc1", "195.11.14.1"),
        "pc1_to_pc2_reachable": ping_ok(runtime, "pc1", "200.1.1.2"),
    }
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
    )
