"""Tests for isolation between concurrent agent sandboxes / MCP gateways."""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from agent.sandbox.sbx.policy import sanitize_sandbox_name
from nika.mcp.gateway.lifecycle import McpGatewayManager
from nika.utils.net import pick_free_port
from tests.agent.sandbox_support import (
    run_cross_sandbox_isolation_probe,
    sandbox_runtime_available,
)


def test_sandbox_names_are_unique_per_session() -> None:
    a = sanitize_sandbox_name("20260724-120000-test-aaaaaa")
    b = sanitize_sandbox_name("20260724-120000-test-bbbbbb")
    assert a != b
    assert a.startswith("nika-")
    assert b.startswith("nika-")


def test_sandbox_names_fit_linux_hostname_limit() -> None:
    long_id = (
        "isp_nobel-eu__bgp_acl_block__m__isis__ibgp_rr__kathara__frr__"
        "host_name-amsterdam__t01"
    )
    name = sanitize_sandbox_name(long_id)
    assert len(name) <= 63
    assert name.startswith("nika-")
    other = sanitize_sandbox_name(long_id.replace("amsterdam", "brussels"))
    assert name != other
    assert len(other) <= 63


def test_concurrent_gateways_bind_distinct_ephemeral_ports() -> None:
    """Host gateways for parallel agent runs must not share a listen port."""
    port_a = pick_free_port("127.0.0.1")
    port_b = pick_free_port("127.0.0.1")
    assert port_a != port_b

    managers = [
        McpGatewayManager(host="127.0.0.1", port=port_a),
        McpGatewayManager(host="127.0.0.1", port=port_b),
    ]
    try:
        for manager in managers:
            manager.start()
        assert managers[0].port != managers[1].port
        for manager in managers:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{manager.port}/gateway/health",
                timeout=2,
            ) as resp:
                assert resp.status == 200
    finally:
        for manager in managers:
            manager.stop()

    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(
            f"http://127.0.0.1:{port_a}/gateway/health",
            timeout=1,
        )


@pytest.mark.skipif(
    not sandbox_runtime_available(),
    reason="Docker Sandboxes runtime not available",
)
class SandboxCrossIsolationTest:
    def test_unallowed_mcp_gateway_port_is_blocked(self) -> None:
        """Sandbox may reach its allowed gateway port, not a peer session port."""
        run_cross_sandbox_isolation_probe()
