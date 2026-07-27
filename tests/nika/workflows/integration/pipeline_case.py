from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import ClassVar

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from agent.utils.mcp_servers import MCPServerConfig
from agent.utils.phases import DIAGNOSIS, SUBMISSION
from nika.cli.utils import env_id_from_lab
from nika.utils.session_store import SessionStore
from nika.workflows.eval.session import run_eval_metrics
from nika.workflows.eval.summary import run_eval_summary
from nika.workflows.failure.inject import inject_failure
from nika.workflows.session.close import close_session
from nika.workflows.session.containers import list_session_containers
from tests.support.integration_base import (
    CliIntegrationTestCase,
    OrderedPipelineTestCase,
)
from tests.support.integration_pipeline import tool_text_list


class PipelineCaseBase(CliIntegrationTestCase, OrderedPipelineTestCase):
    """Parameterized end-to-end pipeline: env → inject → MCP → mock agent → close → eval."""

    __test__ = False

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls is not PipelineCaseBase:
            cls.__test__ = True
    SCENARIO: ClassVar[str]
    BACKEND: ClassVar[str] = "kathara"
    ENV_RUN_ARGS: ClassVar[list[str]] = []
    PROBLEM: ClassVar[str] = "link_down"
    INJECT_PARAMS: ClassVar[dict[str, str]]
    EXPECTED_NODES: ClassVar[frozenset[str]]
    EXEC_PROBE_HOST: ClassVar[str]
    EXEC_PROBE_CMD: ClassVar[str] = "hostname"
    SUBMIT_FAULTY_DEVICES: ClassVar[list[str]]
    ROOT_CAUSE_CATEGORY: ClassVar[str] = "link_failure"
    IMAGE_SUBSTRING: ClassVar[str | None] = "kathara"
    DIAGNOSIS_MCP_SERVERS: ClassVar[list[str]] = ["kathara_base_mcp_server"]

    async def _extra_diagnosis_mcp_checks(self, tools: dict) -> dict[str, str]:
        return {}

    def test_step_01_start_env(self) -> None:
        list_output = self._invoke_ok(["env", "list"])

        assert self.SCENARIO in list_output
        type(self).session_id = self._start_env(self.SCENARIO, self.ENV_RUN_ARGS)
        row = self._assert_session_ready(self.session_id, self.SCENARIO)
        if self.BACKEND != "kathara":
            assert row.get("backend") == self.BACKEND

    def test_step_02_verify_session_and_cli(self) -> None:
        assert self.session_id is not None
        row = SessionStore().get_session(self.session_id)

        assert row["status"] == "running"

        assert row["scenario_name"] == self.SCENARIO
        lab_name = row.get("lab_name")
        assert lab_name is not None
        ps_output = self._invoke_ok(["env", "ps"])

        assert env_id_from_lab(lab_name) in ps_output

        assert self.SCENARIO in ps_output

        assert "1 active" in ps_output
        resolved_id, resolved_lab, container_rows = list_session_containers(
            self.session_id
        )

        assert resolved_id == self.session_id

        assert resolved_lab == lab_name

        assert len(container_rows) == len(self.EXPECTED_NODES)

        assert {r["name"] for r in container_rows} == self.EXPECTED_NODES
        for container_row in container_rows:
            assert container_row["status"] == "running"

            assert re.search("^[0-9a-f]{12}$", container_row["container_id"])
            if self.IMAGE_SUBSTRING:
                assert self.IMAGE_SUBSTRING in container_row["image"].lower()
        exec_output = self._invoke_ok(
            [
                "exec",
                "--session_id",
                self.session_id,
                self.EXEC_PROBE_HOST,
                self.EXEC_PROBE_CMD,
            ]
        )

        assert exec_output.strip()
        describe_output = self._invoke_ok(["failure", "describe", self.PROBLEM])

        assert self.PROBLEM in describe_output

    def test_step_03_inject_failure(self) -> None:
        assert self.session_id is not None
        inject_failure(
            [self.PROBLEM],
            session_id=self.session_id,
            param_overrides=dict(self.INJECT_PARAMS),
        )
        self._assert_failure_injected(self.PROBLEM)
        row = SessionStore().get_session(self.session_id)

        assert self.PROBLEM in row.get("problem_names", [])
        type(self).session_dir = Path(row["session_dir"])
        ground_truth = self._load_json("ground_truth.json")

        assert ground_truth["is_anomaly"]

        assert self.PROBLEM in ground_truth["root_cause_name"]

        assert ground_truth["root_cause_category"] == self.ROOT_CAUSE_CATEGORY
        for device in self.SUBMIT_FAULTY_DEVICES:
            assert device in ground_truth["faulty_devices"]

    def test_step_04_mcp_session_context(self) -> None:
        assert self.session_id is not None
        row = SessionStore().get_session(self.session_id)
        prev = os.environ.get("NIKA_SESSION_ID")
        try:
            os.environ["NIKA_SESSION_ID"] = self.session_id
            from nika.service.mcp_server.mcp_session_context import (
                get_lab_name,
                get_session_dir,
                require_session_id,
            )

            assert require_session_id() == self.session_id

            assert get_lab_name() == row["lab_name"]

            assert get_session_dir() == row["session_dir"]
        finally:
            if prev is None:
                os.environ.pop("NIKA_SESSION_ID", None)
            else:
                os.environ["NIKA_SESSION_ID"] = prev

    def test_step_05_diagnosis_mcp_tools(self) -> None:
        assert self.session_id is not None
        from nika.service.mcp_gateway.lifecycle import mcp_gateway_for_session

        with mcp_gateway_for_session(self.session_id, scenario_name=self.SCENARIO):
            mcp_config = MCPServerConfig(session_id=self.session_id)
            diagnosis_config = mcp_config.load_http_config(self.DIAGNOSIS_MCP_SERVERS)

            async def _run() -> dict:
                client = MultiServerMCPClient(connections=diagnosis_config)
                tools = {t.name: t for t in await client.get_tools()}
                reach = await tools["get_reachability"].ainvoke({})
                host_cfg = await tools["get_host_net_config"].ainvoke(
                    {"host_name": self.EXEC_PROBE_HOST}
                )
                exec_out = await tools["exec_shell"].ainvoke(
                    {"host_name": self.EXEC_PROBE_HOST, "command": self.EXEC_PROBE_CMD}
                )
                extra = await self._extra_diagnosis_mcp_checks(tools)
                return {
                    "reachability": str(reach),
                    "host_net_config": str(host_cfg),
                    "exec_shell": str(exec_out),
                    **extra,
                }

            results = asyncio.run(_run())
        for key, output in results.items():
            assert len(output) > 0, f"{key} must return non-empty output"

            assert "NIKA_SESSION_ID is not set" not in output

    def test_step_06_submit_via_mcp(self) -> None:
        assert self.session_id is not None
        assert self.session_dir is not None
        from nika.service.mcp_gateway.lifecycle import mcp_gateway_for_session
        from nika.service.mcp_gateway.phase import advance_mcp_phase
        from agent.utils.phases import SUBMISSION

        with mcp_gateway_for_session(self.session_id, scenario_name=self.SCENARIO):
            advance_mcp_phase(self.session_id, SUBMISSION)
            config = MCPServerConfig(session_id=self.session_id).load_http_config(
                ["task_mcp_server"]
            )

            async def _run() -> str:
                client = MultiServerMCPClient(connections=config)
                tools = {t.name: t for t in await client.get_tools()}
                submit_result = await tools["submit"].ainvoke(
                    {
                        "is_anomaly": True,
                        "faulty_devices": self.SUBMIT_FAULTY_DEVICES,
                        "root_cause_name": [self.PROBLEM],
                    }
                )
                return str(submit_result)

            result_str = asyncio.run(_run())

        assert "success" in result_str.lower()
        submission = self._load_json("submission.json")

        assert submission["is_anomaly"]
        for device in self.SUBMIT_FAULTY_DEVICES:
            assert device in submission["faulty_devices"]

    def test_step_07_run_mock_agent(self) -> None:
        assert self.session_id is not None
        self._run_agent(agent_type="mock", model="mock-v1", max_steps=20)
        for name in (
            "ground_truth.json",
            "messages.jsonl",
            "submission.json",
            "run.json",
        ):
            assert (self.session_dir / name).exists(), f"missing {name}"
        messages = self._load_jsonl("messages.jsonl")
        agents = {entry["agent"] for entry in messages}

        assert DIAGNOSIS in agents

        assert SUBMISSION in agents

    def test_step_08_session_close(self) -> None:
        assert self.session_id is not None
        close_session(session_id=self.session_id)
        type(self).env_destroyed = True
        run = self._load_json("run.json")

        assert run["status"] == "finished"
        with pytest.raises(FileNotFoundError):
            SessionStore().get_session(self.session_id)

    def test_step_09_eval_metrics(self) -> None:
        assert self.session_id is not None
        run_eval_metrics(session_id=self.session_id)
        metrics = self._load_json("eval_metrics.json")

        assert metrics["detection_score"] == 1.0

        assert metrics["rca_accuracy"] == 1.0

        assert metrics["tool_calls"] > 0
        assert not (self.session_dir / "llm_judge.json").exists()

    def test_step_10_eval_summary(self) -> None:
        """LLM judge / CSV summary belong to ``nika eval``, not the experiment run."""
        assert self.session_id is not None
        assert self.session_dir is not None
        results_root = self.session_dir.parent
        out = run_eval_summary(
            results_dir=str(results_root),
            session_ids=[self.session_id],
        )
        assert out.is_file()
        assert self.session_id in out.read_text(encoding="utf-8")
        assert not (self.session_dir / "llm_judge.json").exists()
