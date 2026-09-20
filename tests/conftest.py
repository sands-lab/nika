from __future__ import annotations

import fcntl
import faulthandler
import sys
from pathlib import Path

import pytest

from tests.support.integration_pipeline import load_test_env
from tests.support.test_scenarios import register_test_scenarios

load_test_env()
register_test_scenarios()

# Surface stuck tests on CI (unit+contract job has hung at ~30% without a node id).
faulthandler.enable(file=sys.__stderr__, all_threads=True)
faulthandler.dump_traceback_later(120, repeat=True, file=sys.__stderr__)


def pytest_runtest_logstart(nodeid: str, location) -> None:
    # Bypass pytest capture so CI logs show the active node under -q.
    sys.__stderr__.write(f"NIKA_TEST_START {nodeid}\n")
    sys.__stderr__.flush()


def pytest_runtest_logfinish(nodeid: str, location) -> None:
    sys.__stderr__.write(f"NIKA_TEST_END {nodeid}\n")
    sys.__stderr__.flush()

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


def pytest_collection_modifyitems(config, items):
    """Apply tier markers from path/name conventions when not explicitly set."""
    for item in items:
        if item.get_closest_marker("unit") or item.get_closest_marker("contract"):
            continue
        path = str(item.fspath)
        name = item.nodeid
        norm_path = path.replace("\\", "/")

        if "tests/ci/" in norm_path:
            if not item.get_closest_marker("ci_smoke"):
                item.add_marker(pytest.mark.ci_smoke)
            continue

        if any(
            token in path
            for token in (
                "test_sandbox_security",
                "test_sandbox_isolation",
                "test_sandbox_benchmark",
            )
        ):
            item.add_marker(pytest.mark.sandbox)
            continue

        if (
            any(
                token in path
                for token in (
                    "workflows/integration",
                    "test_batch.py",
                    "test_sandbox_agents",
                    "leaderboard/test_e2e",
                )
            )
            or "Pipeline" in name
            or "pipeline" in path
        ):
            item.add_marker(pytest.mark.e2e)
            continue

        if (
            "_live" in path
            or "test_bgp_rpki_invalid" in path
            or "test_bgp_max_prefix" in path
        ):
            if "PipelineCaseBase" not in name:
                item.add_marker(pytest.mark.live)
            continue

        if any(
            token in path
            for token in (
                "benchmark/test_",
                "test_resource_mapping",
                "test_compatibility",
                "test_alias_load",
                "test_inject_resolve",
                "test_migrate",
                "test_multi_fault_benchmark_row",
                "test_isp_options",
                "test_isp_bgp_symptom",
                "test_isp_contract",
                "test_healthy_cases",
                "test_validation_contract",
                "test_pack_validate",
                "test_submit_validation",
                "test_symptom_contracts",
                "leaderboard/test_submit_unit",
            )
        ):
            item.add_marker(pytest.mark.contract)
            continue

        if any(
            token in path
            for token in (
                "_docker",
                "_integration",
                "failure_inject",
                "failure_compat",
                "test_kathara_api_smoke",
                "service/pingmesh/test_integration",
                "test_mcp_access",
                "test_k8s_mcp",
            )
        ) or item.get_closest_marker("integration"):
            item.add_marker(pytest.mark.integration)
            continue

        if "_unit" in path or path.endswith("test_scoring.py"):
            item.add_marker(pytest.mark.unit)
            continue

        # Default fast tier for remaining pure-python tests.
        item.add_marker(pytest.mark.unit)
