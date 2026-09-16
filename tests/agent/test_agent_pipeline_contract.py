"""Parametrized host-side agent pipeline contract (BYO agents).

Includes background traffic before the live agent so diagnosis runs under load.
"""

from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from nika.cli.main import app
from nika.run_config.loader import reset_run_config, set_run_config
from nika.run_config.schema import RunConfig
from nika.utils.session_store import SessionStore
from tests.agent._assertions import assert_phase_messages
from tests.support.integration_base import OrderedPipelineTestCase
from tests.support.integration_pipeline import (
    ClabCommonPipelineSteps,
    CommonPipelineSteps,
    _min3clos_prerequisites,
    deepseek_api_key_available,
    load_test_env,
)

load_test_env()

DEEPSEEK_FLASH = "deepseek-v4-flash"
BYO_MAX_STEPS = 20
_TRAFFIC_ARGS = [
    "traffic",
    "run",
    "od",
    "--mesh-mbps",
    "1",
    "--interval",
    "3",
    "--background",
]


def _run_background_od_traffic(session_id: str) -> None:
    prev = os.environ.get("NIKA_SESSION_ID")
    os.environ["NIKA_SESSION_ID"] = session_id
    try:
        result = CliRunner().invoke(app, _TRAFFIC_ARGS)
        assert result.exit_code == 0, result.output
    finally:
        if prev is None:
            os.environ.pop("NIKA_SESSION_ID", None)
        else:
            os.environ["NIKA_SESSION_ID"] = prev


def _set_yaml_agent_config(
    *,
    agent_type: str,
    provider: str = "deepseek",
    model: str = DEEPSEEK_FLASH,
    max_steps: int = BYO_MAX_STEPS,
) -> None:
    reset_run_config()
    set_run_config(
        RunConfig.model_validate(
            {
                "agent": {
                    "type": agent_type,
                    "provider": provider,
                    "model": model,
                    "max_steps": max_steps,
                    # Keep host config/nika.yaml custom.base_url from hijacking providers.
                    "custom": {"base_url": None, "model": None},
                }
            }
        )
    )


class _ByoAgentPipelineBase(CommonPipelineSteps, OrderedPipelineTestCase):
    agent_type: str = ""
    llm_provider: str = "deepseek"
    yaml_model: str = DEEPSEEK_FLASH

    def setup_method(self) -> None:
        _set_yaml_agent_config(
            agent_type=self.agent_type,
            provider=self.llm_provider,
            model=self.yaml_model,
            max_steps=BYO_MAX_STEPS,
        )

    def teardown_method(self) -> None:
        reset_run_config()

    def test_step_01_start_env(self) -> None:
        self._step_start_env()

    def test_step_02_inject_failure(self) -> None:
        self._step_inject_failure()

    def test_step_02b_traffic_run(self) -> None:
        assert self.session_id is not None
        _run_background_od_traffic(self.session_id)

    def test_step_03_run_agent_from_yaml(self) -> None:
        assert self.session_id is not None
        self._run_agent(
            agent_type=self.agent_type,
            llm_provider=None,
            model=None,
            max_steps=None,
        )
        row = SessionStore().get_session(self.session_id)
        assert row.get("agent_type") == self.agent_type
        assert row.get("model") == self.yaml_model

    def test_step_04_check_messages(self) -> None:
        assert self.session_dir is not None
        assert_phase_messages(
            self._load_jsonl("messages.jsonl"),
            require_submission_tools=False,
        )

    def test_step_05_session_close(self) -> None:
        self._step_close_and_verify(self.agent_type)


class _McpAgentClabPipelineBase(ClabCommonPipelineSteps, OrderedPipelineTestCase):
    agent_type: str = "byo.mcp_agent"
    llm_provider: str = "deepseek"
    yaml_model: str = DEEPSEEK_FLASH

    def setup_method(self) -> None:
        _set_yaml_agent_config(
            agent_type=self.agent_type,
            provider=self.llm_provider,
            model=self.yaml_model,
        )

    def teardown_method(self) -> None:
        reset_run_config()

    def test_step_01_start_env(self) -> None:
        self._step_start_env()

    def test_step_02_inject_failure(self) -> None:
        self._step_inject_failure()

    def test_step_02b_traffic_run(self) -> None:
        assert self.session_id is not None
        _run_background_od_traffic(self.session_id)

    def test_step_03_run_agent_from_yaml(self) -> None:
        assert self.session_id is not None
        self._run_agent(
            agent_type=self.agent_type,
            llm_provider=None,
            model=None,
            max_steps=None,
        )
        row = SessionStore().get_session(self.session_id)
        assert row.get("agent_type") == self.agent_type

    def test_step_04_check_messages(self) -> None:
        assert self.session_dir is not None
        assert_phase_messages(
            self._load_jsonl("messages.jsonl"),
            require_submission_tools=False,
        )

    def test_step_05_session_close(self) -> None:
        self._step_close_and_verify(self.agent_type)


@pytest.mark.live
@pytest.mark.e2e
@pytest.mark.skipif(
    not deepseek_api_key_available(),
    reason="DEEPSEEK_API_KEY required for BYO agent live e2e",
)
class LanggraphKatharaPipelineTest(_ByoAgentPipelineBase):
    agent_type = "byo.langgraph"


@pytest.mark.live
@pytest.mark.e2e
@pytest.mark.skipif(
    not deepseek_api_key_available(),
    reason="DEEPSEEK_API_KEY required for BYO agent live e2e",
)
class McpAgentKatharaPipelineTest(_ByoAgentPipelineBase):
    agent_type = "byo.mcp_agent"


@pytest.mark.live
@pytest.mark.e2e
@pytest.mark.skipif(
    not deepseek_api_key_available(),
    reason="DEEPSEEK_API_KEY required for BYO agent live e2e",
)
class AutogenKatharaPipelineTest(_ByoAgentPipelineBase):
    agent_type = "byo.autogen"


@pytest.mark.live
@pytest.mark.e2e
@pytest.mark.skipif(
    not (_min3clos_prerequisites() and deepseek_api_key_available()),
    reason="containerlab/gnmic/Docker or DEEPSEEK_API_KEY not available",
)
class McpAgentClabPipelineTest(_McpAgentClabPipelineBase):
    pass
