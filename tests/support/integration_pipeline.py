from __future__ import annotations
import json
import os
import shutil
from pathlib import Path
from nika.utils.session_index import SessionIndex
from nika.utils.session_store import SessionStore
from nika.workflows.eval.session import run_eval_metrics
from nika.workflows.eval.summary import run_eval_summary
from nika.workflows.session.close import close_session
from tests.support.prerequisites import containerlab_prerequisites

SCENARIO = "simple_bgp"
PROBLEM = "link_down"
LINK_INJECT_PARAMS = {"host_name": "pc1", "intf_name": "eth0"}
CLAB_SCENARIO = "min3clos"
CLAB_LINK_INJECT_PARAMS = {"host_name": "leaf1", "intf_name": "e1-1"}
_min3clos_prerequisites = containerlab_prerequisites


def load_test_env() -> None:
    """Load ``.env`` from the repository root (idempotent)."""
    from dotenv import load_dotenv

    repo_root = Path(__file__).resolve().parents[1]
    load_dotenv(repo_root / ".env")


def openai_api_key_available() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def codex_cli_available() -> bool:
    if shutil.which("codex") is None:
        return False
    from agent.sandbox.sbx.credentials import sbx_openai_credential_available

    return (
        sbx_openai_credential_available()
        or (Path.home() / ".codex" / "auth.json").is_file()
    )


def claude_cli_available() -> bool:
    from agent.local_cli.claude_cli.config import claude_credentials_available

    return claude_credentials_available()


def deepseek_api_key_available() -> bool:
    return bool(os.environ.get("DEEPSEEK_API_KEY"))


def sade_available() -> bool:
    try:
        import claude_agent_sdk
    except ImportError:
        return False
    from agent.community.sade.config import sade_credentials_available

    return sade_credentials_available()


def claude_sdk_available() -> bool:
    try:
        import claude_agent_sdk
    except ImportError:
        return False
    from agent.sdk.claude_sdk.config import claude_sdk_credentials_available

    return claude_sdk_credentials_available()


def codex_sdk_available() -> bool:
    try:
        import openai_codex
    except ImportError:
        return False
    from agent.sdk.codex_sdk.config import codex_sdk_local_auth_available

    return codex_sdk_local_auth_available()


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


class CommonPipelineSteps:
    """Mixin with shared step helpers for ordered pipeline test cases."""

    def _step_start_env(self) -> None:
        type(self).session_id = self._start_env(SCENARIO)
        self._assert_session_ready(self.session_id, SCENARIO)

    def _step_inject_failure(self) -> None:
        assert self.session_id is not None
        self._inject_failure(PROBLEM, LINK_INJECT_PARAMS)
        row = SessionStore().get_session(self.session_id)

        assert PROBLEM in row.get("problem_names", [])

        assert "task_description" in row
        type(self).session_dir = Path(row["session_dir"])
        gt = json.loads((type(self).session_dir / "ground_truth.json").read_text())

        assert gt["is_anomaly"]

        assert PROBLEM in gt["root_cause_name"]

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
        # Trials nest under trials/; summary scans the results root.
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


class ClabCommonPipelineSteps:
    """Mixin with shared step helpers for containerlab min3clos pipeline tests."""

    def _step_start_env(self) -> None:
        type(self).session_id = self._start_env(CLAB_SCENARIO)
        self._assert_session_ready(self.session_id, CLAB_SCENARIO)

    def _step_inject_failure(self) -> None:
        assert self.session_id is not None
        self._inject_failure(PROBLEM, CLAB_LINK_INJECT_PARAMS)
        row = SessionStore().get_session(self.session_id)

        assert PROBLEM in row.get("problem_names", [])

        assert "task_description" in row
        type(self).session_dir = Path(row["session_dir"])
        gt = json.loads((type(self).session_dir / "ground_truth.json").read_text())

        assert gt["is_anomaly"]

        assert PROBLEM in gt["root_cause_name"]

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
