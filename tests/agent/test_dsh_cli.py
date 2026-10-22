from __future__ import annotations

import json
import os
import unittest.mock

import pytest
import yaml

from agent.cli.dsh.config import dsh_env, map_dsh_effort, resolve_dsh_model
from agent.cli.dsh.dsh_worker import (
    DshMaxStepsReached,
    DshWorker,
    build_headless_patch,
    is_model_response_start,
    sanitize_mcp_server_name,
)
from agent.protocols import DIAGNOSIS, SUBMISSION

pytestmark = pytest.mark.unit


class DshMcpPatchTest:
    """Headless-profile patch generation for DeepSeek Harness MCP clients."""

    def test_http_servers_become_streamable_http_entries(self) -> None:
        patch = build_headless_patch(
            {
                "kathara_base_mcp_server": {
                    "transport": "http",
                    "url": "http://gw:9000/mcp/kathara_base_mcp_server/mcp",
                    "headers": {"X-Session-Id": "sess", "X-Session-Token": "tok"},
                }
            },
            model="deepseek-v4-flash",
            base_url="http://127.0.0.1:9",
        )
        ops = yaml.safe_load(patch)
        inserted = next(op["insert"] for op in ops if "insert" in op)
        assert len(inserted) == 1
        row = inserted[0]
        assert row["id"] == "mcp-kathara_base"
        assert row["name"] == "@deepseek-ai/dsh-mcp-client"
        assert row["config"]["serverName"] == "kathara_base"
        assert len(row["config"]["serverName"]) <= 32
        assert row["config"]["transport"] == "streamable-http"
        assert row["config"]["url"] == "http://gw:9000/mcp/kathara_base_mcp_server/mcp"
        assert row["config"]["headers"]["X-Session-Id"] == "sess"
        assert row["config"]["failOnStartupError"] is True

    def test_shortens_task_server_name(self) -> None:
        patch = build_headless_patch(
            {
                "task_mcp_server": {
                    "transport": "http",
                    "url": "http://gw:9000/mcp/task_mcp_server/mcp",
                }
            },
            model="deepseek-v4-flash",
        )
        ops = yaml.safe_load(patch)
        inserted = next(op["insert"] for op in ops if "insert" in op)
        assert inserted[0]["config"]["serverName"] == "task"
        assert inserted[0]["id"] == "mcp-task"

    def test_server_names_are_unique_after_sanitize(self) -> None:
        used: set[str] = set()
        first = sanitize_mcp_server_name("kathara_base_mcp_server", used)
        second = sanitize_mcp_server_name("kathara_base", used)
        assert first == "kathara_base"
        assert second != first
        assert len(second) <= 32

    def test_points_llm_plugins_at_proxy_not_public_api(self) -> None:
        patch = build_headless_patch(
            {},
            model="deepseek-v4-flash",
            base_url="http://127.0.0.1:18080/v1",
            reasoning_effort="medium",
        )
        assert "api.deepseek.com" not in patch
        ops = yaml.safe_load(patch)
        by_id = {op["id"]: op for op in ops if "id" in op}
        assert "baseURL" not in by_id["llm-deepseek"]["config"]
        assert by_id["llm-deepseek"]["config"]["apiKeyEnv"] == "DEEPSEEK_API_KEY"
        proxy = by_id["llm-pi-ai"]["config"]["providers"]["proxy"]
        assert proxy["api"] == "openai-completions"
        assert proxy["baseURL"] == "http://127.0.0.1:18080/v1"
        assert proxy["apiKeyEnv"] == "DEEPSEEK_API_KEY"
        assert proxy["models"] == [{"id": "deepseek-v4-flash"}]
        assert by_id["agent-default-model"]["config"]["provider"] == "proxy"
        assert by_id["agent-default-model"]["config"]["model"] == "deepseek-v4-flash"
        assert by_id["agent-default-model"]["config"]["reasoningEffort"] == "high"

    def test_omits_reasoning_when_unset_or_none(self) -> None:
        patch = build_headless_patch({}, model="m", reasoning_effort=None)
        ops = yaml.safe_load(patch)
        by_id = {op["id"]: op for op in ops if "id" in op}
        assert "reasoningEffort" not in by_id["llm-deepseek"]["config"]
        assert "reasoningEffort" not in by_id["agent-default-model"]["config"]
        none_patch = build_headless_patch({}, model="m", reasoning_effort="none")
        none_ops = yaml.safe_load(none_patch)
        none_by_id = {op["id"]: op for op in none_ops if "id" in op}
        assert "reasoningEffort" not in none_by_id["agent-default-model"]["config"]


class DshEnvTest:
    """Credential aliases and constructor validation."""

    def test_aliases_openai_credentials_for_dsh(self, tmp_path) -> None:
        with unittest.mock.patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "proxy-token",
                "OPENAI_BASE_URL": "http://127.0.0.1:9",
            },
            clear=True,
        ):
            env = dsh_env(dsh_home=tmp_path / "home", session_dir=tmp_path)
        assert env["DEEPSEEK_API_KEY"] == "proxy-token"
        assert env["DEEPSEEK_BASE_URL"] == "http://127.0.0.1:9"
        assert env["DSH_HOME"] == str(tmp_path / "home")
        assert env["DSH_PERMISSION_MODE"] == "danger-full-access"
        assert env["DSH_TELEMETRY_DISABLED"] == "1"
        assert str(tmp_path / ".npm-prefix" / "bin") in env["PATH"].split(os.pathsep)

    def test_rejects_missing_model(self) -> None:
        with pytest.raises(ValueError):
            resolve_dsh_model(None)
        with pytest.raises(ValueError):
            resolve_dsh_model("  ")

    def test_maps_reasoning_effort(self) -> None:
        assert map_dsh_effort(None) is None
        assert map_dsh_effort("none") is None
        assert map_dsh_effort("low") == "low"
        assert map_dsh_effort("medium") == "high"
        assert map_dsh_effort("xhigh") == "max"
        with pytest.raises(ValueError):
            map_dsh_effort("turbo")


class DshWorkerConfigTest:
    """DshWorker constructor validation and phase MCP servers."""

    def test_rejects_invalid_phase(self) -> None:
        with pytest.raises(ValueError):
            DshWorker(
                session_id="sess-123",
                session_dir="/tmp/sess-123",
                phase="invalid_phase",
                model="deepseek-v4-flash",
                llm_provider="deepseek",
            )

    @pytest.mark.parametrize(
        ("phase", "expected_server_name"),
        [
            (DIAGNOSIS, "kathara_base"),
            (SUBMISSION, "task"),
        ],
    )
    def test_mcp_config_exposes_only_current_phase_servers(
        self, tmp_path, agent_task, phase: str, expected_server_name: str
    ) -> None:
        agent_task()
        worker = DshWorker(
            session_id="sess-123",
            session_dir=str(tmp_path),
            phase=phase,
            model="deepseek-v4-flash",
            llm_provider="deepseek",
        )
        worker.workspace.mkdir()
        worker._write_profile_patch()

        ops = yaml.safe_load(worker._patch_path.read_text())
        inserted = next(op["insert"] for op in ops if "insert" in op)
        names = {row["config"]["serverName"] for row in inserted}
        assert names == {expected_server_name}


class DshStepBudgetTest:
    """cli.dsh counts model responses like the other agents' max_steps."""

    def test_step_start_is_a_model_response(self) -> None:
        assert is_model_response_start({"type": "status", "phase": "step_start"})
        assert not is_model_response_start({"type": "status", "phase": "turn_start"})
        assert not is_model_response_start({"type": "status", "phase": "turn_end"})
        assert not is_model_response_start({"type": "status", "phase": "step_end"})
        assert not is_model_response_start({"type": "text", "text": "hi"})

    def test_turn_and_step_start_count_as_one_response(self, tmp_path) -> None:
        worker = DshWorker(
            session_id="sess-pair",
            session_dir=str(tmp_path),
            phase=DIAGNOSIS,
            model="deepseek-v4-flash",
            llm_provider="deepseek",
            max_steps=1,
            stream_output=False,
        )
        worker._log_dsh_event({"type": "status", "phase": "turn_start", "turn": 1})
        worker._log_dsh_event(
            {"type": "status", "phase": "step_start", "turn": 1, "step": 1}
        )
        worker._log_dsh_event({"type": "text", "text": "partial"})
        worker._log_dsh_event(
            {"type": "status", "phase": "step_end", "turn": 1, "step": 1}
        )
        worker._log_dsh_event({"type": "status", "phase": "turn_end", "turn": 1})
        worker._log_dsh_event({"type": "status", "phase": "turn_start", "turn": 2})
        with pytest.raises(DshMaxStepsReached):
            worker._log_dsh_event(
                {"type": "status", "phase": "step_start", "turn": 2, "step": 1}
            )

    def test_response_past_max_steps_stops_dsh(self, tmp_path) -> None:
        worker = DshWorker(
            session_id="sess-limit",
            session_dir=str(tmp_path),
            phase=DIAGNOSIS,
            model="deepseek-v4-flash",
            llm_provider="deepseek",
            max_steps=1,
            stream_output=False,
        )
        worker._log_dsh_event(
            {"type": "status", "phase": "step_start", "turn": 1, "step": 1}
        )
        worker._log_dsh_event({"type": "text", "text": "partial"})
        worker._log_dsh_event(
            {"type": "status", "phase": "step_end", "turn": 1, "step": 1}
        )
        with pytest.raises(DshMaxStepsReached):
            worker._log_dsh_event(
                {"type": "status", "phase": "step_start", "turn": 2, "step": 1}
            )

    def test_one_llm_end_per_step(self, tmp_path) -> None:
        worker = DshWorker(
            session_id="sess-steps",
            session_dir=str(tmp_path),
            phase=DIAGNOSIS,
            model="deepseek-v4-flash",
            llm_provider="deepseek",
            max_steps=5,
            stream_output=False,
        )
        worker._log_dsh_event(
            {"type": "status", "phase": "step_start", "turn": 1, "step": 1}
        )
        worker._log_dsh_event(
            {
                "type": "tool_call",
                "callId": "c1",
                "tool": "show_bgp_summary",
                "input": {"device": "r1"},
            }
        )
        worker._log_dsh_event(
            {
                "type": "tool_result",
                "callId": "c1",
                "tool": "show_bgp_summary",
                "status": "ok",
                "result": "neighbor down",
            }
        )
        worker._log_dsh_event(
            {"type": "status", "phase": "step_end", "turn": 1, "step": 1}
        )
        worker._log_dsh_event(
            {"type": "status", "phase": "step_start", "turn": 1, "step": 2}
        )
        worker._log_dsh_event({"type": "text", "text": "BGP is down."})
        worker._log_dsh_event(
            {"type": "status", "phase": "turn_end", "usage": {"input_tokens": 9}}
        )
        rows = [
            json.loads(line)
            for line in (tmp_path / "messages.jsonl").read_text().splitlines()
        ]
        assert [r["event"] for r in rows] == [
            "llm_start",
            "tool_start",
            "tool_end",
            "llm_end",
            "llm_start",
            "llm_end",
        ]
        tools = [r for r in rows if r["event"] in {"tool_start", "tool_end"}]
        assert tools[0]["tool"]["name"] == "show_bgp_summary"
        ends = [r for r in rows if r["event"] == "llm_end"]
        assert ends[-1]["text"] == "BGP is down."
        assert ends[-1]["usage_metadata"]["input_tokens"] == 9
