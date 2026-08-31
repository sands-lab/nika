"""Agent E2E for multi-fault PMTUD black hole (cli.claude + DeepSeek)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nika.workflows.benchmark.inject_resolve import resolve_multi_inject_params
from nika.utils.session_store import SessionStore
from nika.workflows.eval.session import run_eval_metrics
from nika.workflows.session.close import close_session
from tests.agent._assertions import assert_phase_messages, assert_submission_fields
from tests.support.integration_base import OrderedPipelineTestCase
from tests.support.integration_pipeline import (
    claude_cli_available,
    deepseek_api_key_available,
    load_test_env,
)
from tests.support.prerequisites import docker_available

load_test_env()

SCENARIO = "dc_clos"
PROBLEMS = ["mtu_mismatch", "icmp_frag_needed_filter_misconfiguration"]
AGENT_MAX_STEPS = 40


def _inject_params() -> dict[str, dict[str, str]]:
    return resolve_multi_inject_params(PROBLEMS, SCENARIO, "s", seed=42)


def _assert_multi_root_causes_submitted(
    session_dir: Path, expected_fault_types: set[str]
) -> None:
    assert_submission_fields(session_dir)
    submission = json.loads((session_dir / "submission.json").read_text())
    causes = submission.get("root_causes") or []
    assert causes, "submission root_causes empty"
    submitted_types = {item.get("fault_type") for item in causes}
    assert expected_fault_types.issubset(submitted_types), (
        f"expected fault types {sorted(expected_fault_types)}; "
        f"got {json.dumps(causes, ensure_ascii=False)}"
    )


class _MultiFaultPmtudAgentPipelineBase(OrderedPipelineTestCase):
    llm_provider: str = ""
    model: str = ""
    agent_type: str = "cli.claude"
    session_id: str | None = None
    session_dir: Path | None = None
    env_destroyed: bool = False
    _params: dict[str, dict[str, str]] | None = None

    def test_step_01_start_env(self) -> None:
        type(self).session_id = self._start_env(SCENARIO, ["-s", "s"])
        self._assert_session_ready(self.session_id, SCENARIO)

    def test_step_02_inject_failure(self) -> None:
        assert self.session_id is not None
        params = _inject_params()
        type(self)._params = params
        self._inject_multi_failure(PROBLEMS, params, session_id=self.session_id)
        self._assert_multi_failure_injected(PROBLEMS, session_id=self.session_id)
        row = SessionStore().get_session(self.session_id)
        type(self).session_dir = Path(row["session_dir"])

    def test_step_03_run_agent(self) -> None:
        assert self.session_id is not None
        self._run_agent(
            agent_type=self.agent_type,
            llm_provider=self.llm_provider,
            model=self.model,
            max_steps=AGENT_MAX_STEPS,
            session_id=self.session_id,
        )
        row = SessionStore().get_session(self.session_id)
        assert row.get("agent_type") == self.agent_type

    def test_step_04_check_messages(self) -> None:
        assert self.session_dir is not None
        messages = self._load_jsonl("messages.jsonl")
        assert_phase_messages(messages, require_submission_tools=True)

    def test_step_05_check_submit_and_eval(self) -> None:
        assert self.session_id is not None
        assert self.session_dir is not None
        _assert_multi_root_causes_submitted(self.session_dir, set(PROBLEMS))
        close_session(session_id=self.session_id)
        type(self).env_destroyed = True
        run_eval_metrics(session_id=self.session_id)
        metrics = self._load_json("eval_metrics.json")
        assert metrics.get("detection_score", 0) >= 1.0
        assert metrics.get("rca_recall", 0) >= 1.0
        assert metrics.get("tool_calls", 0) >= 1


@pytest.mark.skipif(
    not (docker_available() and deepseek_api_key_available() and claude_cli_available()),
    reason="Docker, DEEPSEEK_API_KEY, and Claude CLI required for multi-fault agent e2e",
)
class TestMultiFaultPmtudAgentDeepseek(_MultiFaultPmtudAgentPipelineBase):
    llm_provider = "deepseek"
    model = "deepseek-chat"


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class TestMultiFaultStackedLinkHostMockAgent(OrderedPipelineTestCase):
    """Mock agent regression for link_down + host_missing_ip."""

    agent_type = "mock"
    model = "mock-v1"
    problems = ["link_down", "host_missing_ip"]
    session_id: str | None = None
    session_dir: Path | None = None
    env_destroyed: bool = False

    def test_step_01_start_env(self) -> None:
        type(self).session_id = self._start_env(SCENARIO, ["-s", "s"])
        self._assert_session_ready(self.session_id, SCENARIO)

    def test_step_02_inject_failure(self) -> None:
        assert self.session_id is not None
        params = resolve_multi_inject_params(self.problems, SCENARIO, "s", seed=42)
        self._inject_multi_failure(self.problems, params, session_id=self.session_id)
        self._assert_multi_failure_injected(self.problems, session_id=self.session_id)
        row = SessionStore().get_session(self.session_id)
        type(self).session_dir = Path(row["session_dir"])

    def test_step_03_run_agent(self) -> None:
        assert self.session_id is not None
        self._run_agent(
            agent_type=self.agent_type,
            llm_provider=None,
            model=self.model,
            max_steps=20,
            session_id=self.session_id,
        )

    def test_step_04_check_submit_and_eval(self) -> None:
        assert self.session_id is not None
        assert self.session_dir is not None
        _assert_multi_root_causes_submitted(self.session_dir, set(self.problems))
        close_session(session_id=self.session_id)
        type(self).env_destroyed = True
        run_eval_metrics(session_id=self.session_id)
        metrics = self._load_json("eval_metrics.json")
        assert metrics.get("detection_score", 0) >= 1.0
        assert metrics.get("rca_recall", 0) >= 1.0
