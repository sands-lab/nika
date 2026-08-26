from __future__ import annotations

import fcntl
from pathlib import Path

import pytest

from nika.net_env import net_env_pool
from nika.net_env.net_env_pool import NetEnvSpec
from tests.support.integration_pipeline import load_test_env

load_test_env()

# Keep the small Kathara BGP lab available to backend tests without exposing it
# through the installed scenario registry.
net_env_pool._NET_ENV_SPECS["simple_bgp"] = NetEnvSpec(
    lab_name="simple_bgp",
    module="tests.support.simple_bgp.lab",
    class_name="SimpleBGP",
    tags=("arp", "link", "mac", "bgp", "icmp", "frr", "pc"),
    supported_backends=("kathara",),
)

_SANDBOX_E2E_LOCK = Path("/tmp/nika-sandbox-e2e.lock")


def _is_sandbox_e2e_test(node_path: str) -> bool:
    return (
        "test_sandbox_agents.py" in node_path
        or "test_sandbox_benchmark.py" in node_path
    )


@pytest.fixture(autouse=True)
def sandbox_e2e_serial(request: pytest.FixtureRequest):
    """Serialize sandbox E2E tests that share sbx / MCP gateway resources."""
    node_path = str(request.node.fspath)
    if not _is_sandbox_e2e_test(node_path):
        yield
        return

    _SANDBOX_E2E_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with _SANDBOX_E2E_LOCK.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
