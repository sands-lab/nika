from __future__ import annotations

import json

import pytest

from agent.cli.opencode.opencode_worker import (
    OpenCodeMaxStepsReached,
    OpenCodeWorker,
    build_mcp_section,
    build_opencode_config,
    build_provider_section,
    is_model_response_start,
    map_opencode_variant,
    opencode_model_ref,
)
from agent.protocols import DIAGNOSIS, SUBMISSION

pytestmark = pytest.mark.unit


class OpenCodeMcpJsonTest:
    """MCP config JSON generation for OpenCode CLI."""

    def test_http_servers_are_remote_with_headers(self) -> None:
        mcp = build_mcp_section(
            {
                "kathara_base_mcp_server": {
                    "transport": "http",
                    "url": "http://gw:9000/mcp/kathara_base_mcp_server/mcp",
                    "headers": {
                        "X-Session-Id": "sess",
                        "X-Session-Token": "tok",
                    },
                }
            }
        )
        srv = mcp["kathara_base_mcp_server"]
        assert srv["type"] == "remote"
        assert srv["url"] == "http://gw:9000/mcp/kathara_base_mcp_server/mcp"
        assert srv["enabled"] is True
        assert srv["headers"]["X-Session-Id"] == "sess"
        assert srv["headers"]["X-Session-Token"] == "tok"

    def test_multiple_servers_all_present(self) -> None:
        mcp = build_mcp_section(
            {
                "kathara_base_mcp_server": {
                    "transport": "http",
                    "url": "http://gw/base",
                    "headers": {"X-Session-Id": "sess"},
                },
                "task_mcp_server": {
                    "transport": "http",
                    "url": "http://gw/task",
                    "headers": {"X-Session-Id": "sess"},
                },
            }
        )
        assert set(mcp) == {"kathara_base_mcp_server", "task_mcp_server"}
        assert all(
            entry["type"] == "remote" and entry["enabled"] for entry in mcp.values()
        )

    def test_server_without_headers_omits_headers_key(self) -> None:
        mcp = build_mcp_section(
            {
                "task_mcp_server": {
                    "transport": "http",
                    "url": "http://gw/task",
                }
            }
        )
        assert "headers" not in mcp["task_mcp_server"]


class OpenCodeProviderConfigTest:
    """Custom ``endpoint`` provider routing through sandbox model variables."""

    @pytest.fixture(autouse=True)
    def _clear_provider_env(self, monkeypatch) -> None:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    @pytest.mark.parametrize("provider", ["openai", "deepseek", "custom"])
    def test_openai_compatible_providers_use_openai_sdk(
        self, monkeypatch, provider: str
    ) -> None:
        monkeypatch.setenv("OPENAI_BASE_URL", "http://proxy/v1")
        monkeypatch.setenv("OPENAI_API_KEY", "placeholder")
        section = build_provider_section(
            llm_provider=provider, model="deepseek-v4-flash"
        )
        endpoint = section["endpoint"]
        assert endpoint["npm"] == "@ai-sdk/openai-compatible"
        assert endpoint["name"] == "endpoint"
        assert endpoint["options"]["baseURL"] == "http://proxy/v1"
        assert endpoint["options"]["apiKey"] == "placeholder"
        assert "deepseek-v4-flash" in endpoint["models"]

    def test_anthropic_uses_anthropic_sdk(self, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://proxy")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder")
        section = build_provider_section(
            llm_provider="anthropic", model="claude-sonnet"
        )
        endpoint = section["endpoint"]
        assert endpoint["npm"] == "@ai-sdk/anthropic"
        assert endpoint["options"]["baseURL"] == "http://proxy/v1"
        assert endpoint["options"]["apiKey"] == "placeholder"

    def test_anthropic_keeps_existing_v1_suffix(self, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://proxy/anthropic/v1")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder")
        section = build_provider_section(
            llm_provider="anthropic", model="claude-sonnet"
        )
        assert section["endpoint"]["options"]["baseURL"] == "http://proxy/anthropic/v1"

    def test_max_tokens_sets_context_and_output_limits(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENAI_BASE_URL", "http://proxy/v1")
        monkeypatch.setenv("OPENAI_API_KEY", "placeholder")
        section = build_provider_section(
            llm_provider="custom", model="Qwen3.6-35B-A3B-FP8", max_tokens=32768
        )
        limit = section["endpoint"]["models"]["Qwen3.6-35B-A3B-FP8"]["limit"]
        assert limit["context"] == 128000
        assert limit["output"] == 32768

    def test_model_ref_is_endpoint_prefixed(self) -> None:
        assert opencode_model_ref("deepseek-v4-flash") == "endpoint/deepseek-v4-flash"
        assert opencode_model_ref("endpoint/m1") == "endpoint/m1"

    def test_maps_reasoning_effort_to_variant(self) -> None:
        assert map_opencode_variant(None) is None
        assert map_opencode_variant("none") is None
        assert map_opencode_variant("medium") == "medium"
        assert map_opencode_variant("xhigh") == "max"
        with pytest.raises(ValueError):
            map_opencode_variant("turbo")

    def test_config_disables_autoupdate_and_allows_permissions(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("OPENAI_BASE_URL", "http://proxy/v1")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        config = build_opencode_config(
            {},
            llm_provider="openai",
            model="m1",
        )
        assert config["autoupdate"] is False
        assert config["model"] == "endpoint/m1"
        assert config["permission"] == {"*": "allow"}
        assert "endpoint" in config["provider"]


class OpenCodeWorkerConfigTest:
    """OpenCodeWorker constructor validation and per-phase MCP files."""

    def test_rejects_invalid_phase(self) -> None:
        with pytest.raises(ValueError):
            OpenCodeWorker(
                session_id="sess-123",
                session_dir="/tmp/sess-123",
                phase="invalid_phase",
                llm_provider="openai",
                model="m1",
            )

    @pytest.mark.parametrize(
        ("phase", "expected_servers"),
        [
            (DIAGNOSIS, {"kathara_base_mcp_server"}),
            (SUBMISSION, {"task_mcp_server"}),
        ],
    )
    def test_mcp_config_exposes_only_current_phase_servers(
        self, tmp_path, agent_task, phase: str, expected_servers: set[str]
    ) -> None:
        agent_task()
        worker = OpenCodeWorker(
            session_id="sess-123",
            session_dir=str(tmp_path),
            phase=phase,
            model="test-model",
            llm_provider="openai",
        )
        worker.workspace.mkdir()
        worker._write_config()

        config = json.loads(worker._config_path.read_text())
        assert set(config["mcp"]) == expected_servers
        for srv in config["mcp"].values():
            assert srv["type"] == "remote"
            assert srv["enabled"] is True
            assert "url" in srv
            assert "headers" in srv
            assert srv["timeout"] == 120_000

    def test_setup_workspace_installs_opencode_skills(
        self, tmp_path, agent_task
    ) -> None:
        agent_task(enable_skills=True)
        worker = OpenCodeWorker(
            session_id="sess-123",
            session_dir=str(tmp_path),
            phase=DIAGNOSIS,
            model="test-model",
            llm_provider="openai",
        )
        worker._setup_workspace()
        skills = tmp_path / "opencode_workspace" / ".opencode" / "skills"
        assert skills.is_dir()
        assert not (tmp_path / "opencode_workspace" / "AGENTS.md").exists()


class OpenCodeStepBudgetTest:
    """cli.opencode counts ``step_start`` events as max_steps model responses."""

    @staticmethod
    def _step_start(message_id: str = "msg_1") -> dict:
        return {
            "type": "step_start",
            "part": {"type": "step-start", "messageID": message_id},
        }

    def test_is_model_response_start_helper(self) -> None:
        assert is_model_response_start(self._step_start())
        assert not is_model_response_start({"type": "text", "part": {"text": "hi"}})
        assert not is_model_response_start({"type": "tool_use"})

    def test_one_llm_end_per_step_with_usage(self, tmp_path) -> None:
        worker = OpenCodeWorker(
            session_id="sess-steps",
            session_dir=str(tmp_path),
            phase=DIAGNOSIS,
            llm_provider="openai",
            model="m1",
            max_steps=5,
            stream_output=False,
        )
        worker._handle_stdout_line(json.dumps(self._step_start("msg_a")))
        worker._handle_stdout_line(
            json.dumps(
                {
                    "type": "text",
                    "part": {"type": "text", "text": "checking bgp"},
                }
            )
        )
        worker._handle_stdout_line(
            json.dumps(
                {
                    "type": "step_finish",
                    "part": {
                        "type": "step-finish",
                        "reason": "stop",
                        "tokens": {"input": 10, "output": 4, "reasoning": 0},
                    },
                }
            )
        )
        rows = [
            json.loads(line)
            for line in (tmp_path / "messages.jsonl").read_text().splitlines()
        ]
        events = [r["event"] for r in rows]
        assert events == ["llm_start", "llm_end"]
        assert rows[1]["text"] == "checking bgp"
        assert rows[1]["usage_metadata"]["input_tokens"] == 10
        assert rows[1]["usage_metadata"]["output_tokens"] == 4

    def test_response_past_max_steps_stops_opencode(self, tmp_path) -> None:
        worker = OpenCodeWorker(
            session_id="sess-limit",
            session_dir=str(tmp_path),
            phase=DIAGNOSIS,
            llm_provider="openai",
            model="m1",
            max_steps=1,
            stream_output=False,
        )
        worker._handle_stdout_line(json.dumps(self._step_start("msg_a")))
        worker._handle_stdout_line(
            json.dumps({"type": "text", "part": {"type": "text", "text": "first"}})
        )
        with pytest.raises(OpenCodeMaxStepsReached):
            worker._handle_stdout_line(json.dumps(self._step_start("msg_b")))

    def test_tool_use_emits_canonical_tool_events(self, tmp_path) -> None:
        worker = OpenCodeWorker(
            session_id="sess-tools",
            session_dir=str(tmp_path),
            phase=DIAGNOSIS,
            llm_provider="openai",
            model="m1",
            stream_output=False,
        )
        worker._handle_stdout_line(
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        "type": "tool",
                        "tool": "show_bgp_summary",
                        "callID": "call_1",
                        "state": {
                            "status": "completed",
                            "input": {"device": "router1"},
                            "output": "neighbor down",
                        },
                    },
                }
            )
        )
        rows = [
            json.loads(line)
            for line in (tmp_path / "messages.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        events = [r["event"] for r in rows]
        assert events == ["tool_start", "tool_end"]
        assert rows[0]["tool"]["name"] == "show_bgp_summary"
        assert "neighbor down" in str(rows[1].get("output"))

    def test_tool_use_running_then_completed_is_one_call(self, tmp_path) -> None:
        worker = OpenCodeWorker(
            session_id="sess-tools-stream",
            session_dir=str(tmp_path),
            phase=DIAGNOSIS,
            llm_provider="openai",
            model="m1",
            stream_output=False,
        )
        part = {
            "type": "tool",
            "tool": "show_bgp_summary",
            "callID": "call_1",
        }
        worker._handle_stdout_line(
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        **part,
                        "state": {
                            "status": "running",
                            "input": {"device": "router1"},
                        },
                    },
                }
            )
        )
        worker._handle_stdout_line(
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        **part,
                        "state": {
                            "status": "completed",
                            "input": {"device": "router1"},
                            "output": "neighbor down",
                        },
                    },
                }
            )
        )
        events = [
            json.loads(line)["event"]
            for line in (tmp_path / "messages.jsonl").read_text().splitlines()
            if line.strip()
        ]
        assert events == ["tool_start", "tool_end"]

    def test_stream_error_recovers_assistant_text(self, tmp_path) -> None:
        worker = OpenCodeWorker(
            session_id="sess-recover",
            session_dir=str(tmp_path),
            phase=DIAGNOSIS,
            llm_provider="openai",
            model="m1",
            stream_output=False,
        )
        worker._handle_stdout_line(json.dumps(self._step_start("msg_a")))
        worker._handle_stdout_line(
            json.dumps(
                {"type": "text", "part": {"type": "text", "text": "BGP is down."}}
            )
        )
        worker._handle_stdout_line(
            json.dumps(
                {
                    "type": "error",
                    "error": {"message": "No healthy backend"},
                }
            )
        )
        worker._flush_response()
        result = worker._phase_result(1, "No healthy backend")
        assert result == "BGP is down."
        events = [
            json.loads(line)["event"]
            for line in (tmp_path / "messages.jsonl").read_text().splitlines()
            if line.strip()
        ]
        assert "subprocess_nonzero_recovered" in events
        assert "subprocess_error" not in events

    def test_stream_error_without_text_is_failure(self, tmp_path) -> None:
        worker = OpenCodeWorker(
            session_id="sess-error",
            session_dir=str(tmp_path),
            phase=DIAGNOSIS,
            llm_provider="openai",
            model="m1",
            stream_output=False,
        )
        worker._handle_stdout_line(
            json.dumps(
                {
                    "type": "error",
                    "error": {"message": "No healthy backend"},
                }
            )
        )
        result = worker._phase_result(1, "No healthy backend")
        assert result.startswith("ERROR:")
        assert "No healthy backend" in result
