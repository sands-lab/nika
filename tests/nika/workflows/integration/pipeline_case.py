from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import ClassVar

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from agent.utils.mcp_servers import MCPServerConfig
from agent.protocols import DIAGNOSIS, SUBMISSION
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
    ROOT_CAUSE_CATEGORY: ClassVar[str] = "link_interface"
    IMAGE_SUBSTRING: ClassVar[str | None] = "kathara"
    DIAGNOSIS_MCP_SERVERS: ClassVar[list[str]] = ["kathara_base_mcp_server"]
    RUN_TRAFFIC: ClassVar[bool] = False
    TRAFFIC_RUN_ARGS: ClassVar[list[str]] = [
        "--mesh-mbps",
        "1",
        "--interval",
        "3",
        "--background",
    ]

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

        fault_types = {
            item.get("fault_type") for item in ground_truth.get("root_causes") or []
        }
        assert self.PROBLEM in fault_types

        assert ground_truth["failure_domain"] == self.ROOT_CAUSE_CATEGORY
        assert ground_truth.get("root_causes")
        for device in self.SUBMIT_FAULTY_DEVICES:
            assert any(
                (item.get("resource") or {}).get("node") == device
                or str(item.get("resource_id") or "").startswith(f"node/{device}")
                or str(item.get("resource_id") or "").startswith(f"interface/{device}/")
                or (
                    (item.get("resource") or {}).get("kind") == "link"
                    and f"{device}:"
                    in str((item.get("resource") or {}).get("name") or "")
                )
                or (
                    str(item.get("resource_id") or "").startswith("link/")
                    and f"{device}:" in str(item.get("resource_id") or "")
                )
                for item in ground_truth["root_causes"]
            )

    def test_step_03b_traffic_run(self) -> None:
        if not self.RUN_TRAFFIC:
            pytest.skip("traffic step not enabled for this scenario")
        assert self.session_id is not None
        lab_name = str(self._session_row(self.session_id)["lab_name"])
        self._invoke_ok(
            ["traffic", "run", "od", "--lab", lab_name, *self.TRAFFIC_RUN_ARGS]
        )

    def test_step_04_mcp_session_context(self) -> None:
        assert self.session_id is not None
        row = SessionStore().get_session(self.session_id)
        prev = os.environ.get("NIKA_SESSION_ID")
        try:
            os.environ["NIKA_SESSION_ID"] = self.session_id
            from nika.mcp.session_context import (
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
        from nika.mcp.gateway.lifecycle import mcp_gateway_for_session

        with mcp_gateway_for_session(self.session_id, scenario_name=self.SCENARIO):
            mcp_config = MCPServerConfig(session_id=self.session_id)
            diagnosis_config = mcp_config.load_http_config(self.DIAGNOSIS_MCP_SERVERS)

            async def _run() -> dict:
                client = MultiServerMCPClient(connections=diagnosis_config)
                tools = {t.name: t for t in await client.get_tools()}
                ping = await tools["ping_pair"].ainvoke(
                    {
                        "host_a": self.EXEC_PROBE_HOST,
                        "host_b": self.EXEC_PROBE_HOST,
                        "count": 1,
                    }
                )
                host_cfg = await tools["get_host_net_config"].ainvoke(
                    {"host_name": self.EXEC_PROBE_HOST}
                )
                exec_out = await tools["exec_shell"].ainvoke(
                    {"host_name": self.EXEC_PROBE_HOST, "command": self.EXEC_PROBE_CMD}
                )
                extra = await self._extra_diagnosis_mcp_checks(tools)
                return {
                    "ping_pair": str(ping),
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
        from agent.utils.mcp_client import begin_submission_mcp_phase
        from nika.mcp.gateway.lifecycle import mcp_gateway_for_session
        from nika.workflows.agent.submission import load_submission_context

        report = "integration test frozen diagnosis report"
        with mcp_gateway_for_session(self.session_id, scenario_name=self.SCENARIO):
            begin_submission_mcp_phase(self.session_id, report)
            config = MCPServerConfig(session_id=self.session_id).load_http_config(
                ["task_mcp_server"]
            )

            async def _run() -> str:
                client = MultiServerMCPClient(connections=config)
                tools = {t.name: t for t in await client.get_tools()}
                context = load_submission_context(self.session_id)
                catalog_ids = {
                    str(item["id"])
                    for item in context.get("resources") or []
                    if isinstance(item, dict) and item.get("id")
                }
                assert catalog_ids, (
                    "frozen submission context must include resource ids"
                )
                fault_types = {
                    str(item.get("id")) if isinstance(item, dict) else str(item)
                    for item in context.get("fault_ontology") or []
                }
                assert fault_types, (
                    "frozen submission context must include fault ontology"
                )
                gt = self._load_json("ground_truth.json")
                causes: list[dict[str, str]] = []
                for item in gt.get("root_causes") or []:
                    resource_id = item.get("resource_id") or (
                        item.get("resource") or {}
                    ).get("id")
                    fault_type = str(item.get("fault_type") or self.PROBLEM)
                    if (
                        resource_id
                        and str(resource_id) in catalog_ids
                        and fault_type in fault_types
                    ):
                        causes.append(
                            {
                                "resource_id": str(resource_id),
                                "fault_type": fault_type,
                            }
                        )
                if not causes:
                    causes.append(
                        {
                            "resource_id": sorted(catalog_ids)[0],
                            "fault_type": (
                                self.PROBLEM
                                if self.PROBLEM in fault_types
                                else sorted(fault_types)[0]
                            ),
                        }
                    )
                submit_tool = tools.get("submit")
                if submit_tool is None:
                    for name, tool in tools.items():
                        if name == "submit" or name.endswith("_submit"):
                            submit_tool = tool
                            break
                assert submit_tool is not None, (
                    f"submit tool missing; saw {sorted(tools)}"
                )
                submit_result = await submit_tool.ainvoke(
                    {
                        "is_anomaly": True,
                        "root_causes": causes,
                    }
                )
                return str(submit_result)

            result_str = asyncio.run(_run())

        assert "success" in result_str.lower()
        submission = self._load_json("submission.json")

        assert submission["is_anomaly"]
        assert submission.get("root_causes")
        assert submission["root_causes"][0].get("resource_id") or (
            submission["root_causes"][0].get("resource") or {}
        ).get("id")

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
        phases = {entry["phase"] for entry in messages}

        assert DIAGNOSIS in phases

        assert SUBMISSION in phases

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
