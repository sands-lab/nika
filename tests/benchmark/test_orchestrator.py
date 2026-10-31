"""Pinned-orchestrator releases execute on a checkout, not in the current tree."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from nika.config import REPO_ROOT
from nika.net_env.net_env_pool import list_all_net_envs, resolve_scenario_id
from nika.problems.registry import get_problem_class
from nika.workflows.benchmark.orchestrator import (
    _delegate_argv,
    orchestrator_ref,
    uses_foreign_orchestrator,
)
from nika.workflows.benchmark.release import load_release, preflight_release
from nika.workflows.benchmark.run import run_benchmark_from_release

pytestmark = pytest.mark.contract


def test_0_1_0_pins_a_foreign_orchestrator() -> None:
    release = load_release("0.1.0", split="test")
    assert orchestrator_ref(release.manifest)
    assert uses_foreign_orchestrator(release.manifest)
    assert "p4_counter" in {row["scenario"] for row in release.cases}
    assert "host_vpn_membership_missing" in {row["problem"] for row in release.cases}
    preflight_release(release, check_images=False)


def test_0_1_0_environments_are_absent_from_current_catalogs() -> None:
    assert "p4_counter" not in list_all_net_envs()
    with pytest.raises(ValueError, match="p4_counter"):
        resolve_scenario_id("p4_counter")
    assert get_problem_class("p4_compilation_error_parser_state") is None


def test_delegate_keeps_results_in_the_current_tree() -> None:
    out = _delegate_argv(
        [
            "benchmark",
            "run",
            "--release",
            "0.1.0",
            "--run-config",
            "config/nika.yaml",
            "--result_dir",
            "results/0.1.0-test",
        ]
    )
    assert "--run-config" not in out
    idx = out.index("--result_dir")
    assert Path(out[idx + 1]) == (REPO_ROOT / "results" / "0.1.0-test").resolve()


def test_0_2_0_runs_in_the_current_tree() -> None:
    release = load_release("0.2.0", split="test")
    assert orchestrator_ref(release.manifest) is None
    assert not uses_foreign_orchestrator(release.manifest)


def test_run_delegates_pinned_0_1_0() -> None:
    with patch(
        "nika.workflows.benchmark.orchestrator.delegate_release_run",
        return_value=0,
    ) as delegate:
        with pytest.raises(SystemExit) as exc:
            run_benchmark_from_release(
                "0.1.0",
                "mock",
                None,
                "mock-v1",
                None,
                split="test",
                check_images=False,
            )
        assert exc.value.code == 0
        assert delegate.call_count == 1
        assert delegate.call_args.kwargs["label"] == "nika-bench@0.1.0"
