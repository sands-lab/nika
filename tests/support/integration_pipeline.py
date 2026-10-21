from __future__ import annotations

import json
import os
from pathlib import Path
from typing import ClassVar

from nika.utils.session_index import SessionIndex
from nika.utils.session_store import SessionStore
from nika.workflows.eval.session import run_eval_metrics
from nika.workflows.eval.summary import run_eval_summary
from nika.workflows.session.close import close_session
from tests.support.prerequisites import containerlab_prerequisites

DEFAULT_SCENARIO = "simple_bgp"
DEFAULT_PROBLEM = "link_down"
DEFAULT_INJECT_PARAMS = {"host_name": "pc1", "intf_name": "eth0"}
CLAB_SCENARIO = "min3clos"
CLAB_INJECT_PARAMS = {"host_name": "leaf1", "intf_name": "e1-1"}
_min3clos_prerequisites = containerlab_prerequisites

# Backward-compatible aliases for imports.
SCENARIO = DEFAULT_SCENARIO
PROBLEM = DEFAULT_PROBLEM
LINK_INJECT_PARAMS = DEFAULT_INJECT_PARAMS
CLAB_LINK_INJECT_PARAMS = CLAB_INJECT_PARAMS


def load_test_env() -> None:
    """Load ``.env`` from the repository root (idempotent)."""
    from dotenv import load_dotenv

    repo_root = Path(__file__).resolve().parents[2]
    load_dotenv(repo_root / ".env")


def live_anthropic_test_config() -> tuple[str, str]:
    """Select one provider/model pair for Claude CLI, SDK, and SADE E2E."""
    provider = (
        os.environ.get("NIKA_TEST_ANTHROPIC_PROVIDER", "deepseek").strip().lower()
    )
    if provider not in {"deepseek", "anthropic", "custom"}:
        raise ValueError(
            "NIKA_TEST_ANTHROPIC_PROVIDER must be deepseek, anthropic, or custom"
        )
    model = os.environ.get("NIKA_TEST_ANTHROPIC_MODEL", "").strip()
    if not model:
        if provider != "deepseek":
            raise ValueError(
                "Set NIKA_TEST_ANTHROPIC_MODEL to a model available to your account"
            )
        model = "deepseek-v4-flash"
    return provider, model


def live_custom_base_url() -> str:
    """Endpoint (``agent.base_url``) of the ``custom`` test provider."""
    return os.environ.get("NIKA_TEST_CUSTOM_BASE_URL", "").strip()


def live_codex_test_model() -> str:
    """Keep Codex E2E on OpenAI, with an account-specific model override."""
    return os.environ.get("NIKA_TEST_CODEX_MODEL", "").strip() or "gpt-5-mini"


def live_openai_test_config() -> tuple[str, str]:
    """Provider/model pair for OpenAI-family E2E (Codex and BYO agents).

    ``NIKA_TEST_OPENAI_PROVIDER`` selects ``openai`` (default), ``deepseek``,
    or ``custom`` (``NIKA_TEST_CUSTOM_BASE_URL``); the model is
    ``NIKA_TEST_CODEX_MODEL``.
    """
    provider = os.environ.get("NIKA_TEST_OPENAI_PROVIDER", "openai").strip().lower()
    if provider not in {"openai", "deepseek", "custom"}:
        raise ValueError(
            "NIKA_TEST_OPENAI_PROVIDER must be openai, deepseek, or custom"
        )
    return provider, live_codex_test_model()


def openai_api_key_available() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def codex_cli_available() -> bool:
    """Codex-family agents: an OpenAI key or NIKA's Codex login on the host."""
    from nika.agents.sandbox.subscription import CodexLogin

    return openai_api_key_available() or CodexLogin.load() is not None


def claude_cli_available() -> bool:
    """Claude-family agents: an Anthropic-compatible key or a Claude token."""
    return any(
        os.environ.get(key, "").strip()
        for key in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "DEEPSEEK_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
        )
    )


def deepseek_api_key_available() -> bool:
    return bool(os.environ.get("DEEPSEEK_API_KEY", "").strip())


def anthropic_api_key_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def tool_text_list(result: object) -> list[str]:
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            return [result]
    if not isinstance(result, list):
        return [str(result)]
    return [
        str(item["text"]) if isinstance(item, dict) and "text" in item else str(item)
        for item in result
    ]


class PipelineStepsMixin:
    """Shared ordered pipeline step helpers; subclasses set scenario/inject params."""

    PIPELINE_SCENARIO: ClassVar[str] = DEFAULT_SCENARIO
    PIPELINE_PROBLEM: ClassVar[str] = DEFAULT_PROBLEM
    PIPELINE_INJECT_PARAMS: ClassVar[dict[str, str]] = DEFAULT_INJECT_PARAMS

    def _step_start_env(self) -> None:
        type(self).session_id = self._start_env(self.PIPELINE_SCENARIO)
        self._assert_session_ready(self.session_id, self.PIPELINE_SCENARIO)

    def _step_inject_failure(self) -> None:
        assert self.session_id is not None
        self._inject_failure(self.PIPELINE_PROBLEM, self.PIPELINE_INJECT_PARAMS)
        row = SessionStore().get_session(self.session_id)

        assert self.PIPELINE_PROBLEM in row.get("problem_names", [])

        assert "task_description" in row
        type(self).session_dir = Path(row["session_dir"])
        gt = json.loads((type(self).session_dir / "ground_truth.json").read_text())

        assert gt["is_anomaly"]

        fault_types = {item.get("fault_type") for item in gt.get("root_causes") or []}
        assert self.PIPELINE_PROBLEM in fault_types

    def _step_close_and_verify(self, expected_agent_type: str) -> None:
        assert self.session_id is not None
        close_session(session_id=self.session_id)
        type(self).env_destroyed = True
        run = self._load_json("run.json")

        assert run["status"] == "finished"

        assert run["agent_type"] == expected_agent_type

    def _step_eval_metrics(self, min_tool_calls: int = 1) -> None:
        assert self.session_id is not None
        run_eval_metrics(session_id=self.session_id)
        metrics = self._load_json("eval_metrics.json")
        for field in (
            "detection_score",
            "localization_accuracy",
            "rca_accuracy",
            "tool_calls",
        ):
            assert field in metrics

        assert metrics["detection_score"] >= 0.0

        assert metrics["tool_calls"] >= min_tool_calls
        run = self._load_json("run.json")

        assert "eval_metrics" in run
        index_row = SessionIndex().get_row(self.session_id)
        assert index_row is not None
        assert index_row.get("detection_score") is not None
        self._step_eval_summary()

    def _step_eval_summary(self) -> None:
        """Post-hoc CSV summary via ``nika eval summary`` (not part of agent/benchmark run)."""
        assert self.session_id is not None
        assert self.session_dir is not None
        results_root = self.session_dir.parent
        if results_root.name == "trials":
            results_root = results_root.parent
        out = run_eval_summary(
            results_dir=str(results_root),
            session_ids=[self.session_id],
        )
        assert out.is_file()
        text = out.read_text(encoding="utf-8")
        assert self.session_id in text
        assert not (self.session_dir / "llm_judge.json").exists()


class CommonPipelineSteps(PipelineStepsMixin):
    PIPELINE_SCENARIO = DEFAULT_SCENARIO
    PIPELINE_PROBLEM = DEFAULT_PROBLEM
    PIPELINE_INJECT_PARAMS = DEFAULT_INJECT_PARAMS


class ClabCommonPipelineSteps(PipelineStepsMixin):
    PIPELINE_SCENARIO = CLAB_SCENARIO
    PIPELINE_PROBLEM = DEFAULT_PROBLEM
    PIPELINE_INJECT_PARAMS = CLAB_INJECT_PARAMS
