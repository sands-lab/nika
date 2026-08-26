"""Shared bases for integration tests."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from nika.cli.main import app
from nika.service.mcp_server.mcp_session_context import SESSION_ID_ENV, get_lab_name
from nika.utils.session_id import (
    TEST_SESSION_TAG,
    resolve_session_tag,
    session_id_pattern,
)
from nika.utils.session_store import SessionStore
from nika.workflows.env.start import start_net_env
from nika.workflows.failure.inject import inject_failure as inject_failure_workflow
from nika.workflows.session.close import close_session

TEST_SESSION_ID_RE = session_id_pattern(TEST_SESSION_TAG)


def _parse_env_run_args(extra_args: list[str] | None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    args = list(extra_args or [])
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-s", "--size") and i + 1 < len(args):
            kwargs["topo_size"] = args[i + 1]
            i += 2
        elif arg == "--topo" and i + 1 < len(args):
            kwargs["topo"] = args[i + 1]
            i += 2
        elif arg == "--igp" and i + 1 < len(args):
            kwargs["igp"] = args[i + 1]
            i += 2
        elif arg == "--metric-strategy" and i + 1 < len(args):
            kwargs["metric_strategy"] = args[i + 1]
            i += 2
        elif arg == "--constant-metric" and i + 1 < len(args):
            kwargs["constant_metric"] = int(args[i + 1])
            i += 2
        elif arg == "--bgp-mode" and i + 1 < len(args):
            kwargs["bgp_mode"] = args[i + 1]
            i += 2
        elif arg == "--rpki":
            kwargs["rpki"] = True
            i += 1
        elif arg == "--no-rpki":
            kwargs["rpki"] = False
            i += 1
        elif arg == "--backend" and i + 1 < len(args):
            kwargs["backend"] = args[i + 1]
            i += 2
        elif arg == "--device-profile" and i + 1 < len(args):
            kwargs["device_profile"] = args[i + 1]
            i += 2
        elif arg == "--no-redeploy":
            kwargs["redeploy"] = False
            i += 1
        elif arg == "--instance-tag" and i + 1 < len(args):
            kwargs["instance_tag"] = args[i + 1]
            i += 2
        elif arg == "--result_dir" and i + 1 < len(args):
            kwargs["result_dir"] = args[i + 1]
            i += 2
        else:
            raise ValueError(f"Unsupported env run arg: {arg!r}")
    return kwargs


def _parse_inject_args(extra_args: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    i = 0
    while i < len(extra_args):
        if extra_args[i] == "--set" and i + 1 < len(extra_args):
            key, _, value = extra_args[i + 1].partition("=")
            overrides[key] = value
            i += 2
        else:
            i += 1
    return overrides


class IntegrationMixin:
    """Workflow API helpers and shared session assertions."""

    def _start_env(self, scenario: str, extra_args: list[str] | None = None) -> str:
        kwargs = _parse_env_run_args(extra_args)
        topo_size = kwargs.pop("topo_size", None)
        return start_net_env(
            scenario,
            topo_size,
            session_tag=resolve_session_tag(context="test"),
            **kwargs,
        )

    @classmethod
    def _start_env_class(
        cls, scenario: str, extra_args: list[str] | None = None
    ) -> str:
        kwargs = _parse_env_run_args(extra_args)
        topo_size = kwargs.pop("topo_size", None)
        return start_net_env(
            scenario,
            topo_size,
            session_tag=resolve_session_tag(context="test"),
            **kwargs,
        )

    def _close_session(self, session_id: str) -> None:
        close_session(session_id=session_id)

    @classmethod
    def _close_session_class(cls, session_id: str) -> None:
        close_session(session_id=session_id)

    def _inject_failure(
        self,
        problem: str,
        params: dict[str, str] | None = None,
        *,
        session_id: str | None = None,
    ) -> None:
        sid = session_id or getattr(self, "session_id", None)
        if sid is None:
            raise ValueError("session_id is required")
        inject_params = dict(
            params
            if params is not None
            else self._benchmark_inject_from_yaml(
                self.SCENARIO,
                problem,
                self._topo_size_from_env_args(),
            )
        )
        inject_failure_workflow(
            [problem], session_id=sid, param_overrides=inject_params
        )

    def _run_agent(
        self,
        *,
        agent_type: str,
        model: str | None = None,
        llm_provider: str | None = None,
        max_steps: int | None = None,
        reasoning_effort: str | None = None,
        session_id: str | None = None,
    ) -> None:
        from nika.workflows.agent.run import start_agent

        start_agent(
            agent_type,
            llm_provider,
            model,
            max_steps,
            session_id=session_id or getattr(self, "session_id", None),
            reasoning_effort=reasoning_effort,
            stream_output=False,
        )

    def _session_row(self, session_id: str | None = None) -> dict:
        sid = session_id or getattr(self, "session_id", None)
        if sid is None:
            raise ValueError("session_id is required")
        return SessionStore().get_session(sid)

    def _assert_session_ready(self, session_id: str, scenario: str) -> dict:
        row = self._session_row(session_id)
        assert row["session_id"] == session_id
        assert row["status"] == "running"
        assert row["scenario_name"] == scenario
        assert row.get("lab_name") is not None, "lab_name must be set after env run"
        assert scenario in row["lab_name"]
        assert re.search(
            TEST_SESSION_ID_RE,
            session_id,
        ), f"session_id must match YYYYMMDD-HHMMSS-{TEST_SESSION_TAG}-{{6hex}}"
        if os.environ.get(SESSION_ID_ENV) == session_id:
            assert get_lab_name() == row["lab_name"]
        return row

    def _scenario_kwargs(self, session_id: str | None = None) -> dict:
        row = self._session_row(session_id)
        kwargs = dict(row.get("scenario_params") or {})
        if row.get("lab_name"):
            kwargs["lab_name"] = row["lab_name"]
        if row.get("scenario_topo_size") is not None:
            kwargs["topo_size"] = row["scenario_topo_size"]
        return kwargs

    def _problem(self, cls_, session_id: str | None = None):
        scenario = (
            getattr(self, "SCENARIO", None)
            or self._session_row(session_id)["scenario_name"]
        )
        return cls_(scenario_name=scenario, **self._scenario_kwargs(session_id))

    def _topo_size_from_env_args(self) -> str:
        args = getattr(self, "ENV_RUN_ARGS", [])
        if "-s" in args:
            return args[args.index("-s") + 1]
        return ""

    @staticmethod
    def _benchmark_inject_from_yaml(
        scenario: str,
        problem: str,
        topo_size: str = "",
    ) -> dict[str, str]:
        from tests.benchmark.helpers import (
            inject_params_from_benchmark_yaml,
        )

        return inject_params_from_benchmark_yaml(scenario, problem, topo_size)

    def _assert_failure_injected(
        self, problem: str, session_id: str | None = None
    ) -> None:
        sid = session_id or getattr(self, "session_id", None)
        if sid is None:
            raise ValueError("session_id is required")
        failures = SessionStore().list_failure_injections(session_id=sid)
        matching = [row for row in failures if row.get("problem_name") == problem]
        assert matching, f"No failure record for {problem}"
        assert matching[-1].get("status") == "injected"
        session_row = SessionStore().get_session(sid)
        gt_path = Path(session_row["session_dir"]) / "ground_truth.json"
        gt = json.loads(gt_path.read_text(encoding="utf-8"))
        assert gt.get("is_anomaly") is True
        assert gt.get("root_causes"), f"missing root_causes for {problem}"
        fault_types = {item.get("fault_type") for item in gt.get("root_causes") or []}
        assert problem in fault_types
        for item in gt["root_causes"]:
            resource_id = item.get("resource_id") or (item.get("resource") or {}).get(
                "id", ""
            )
            assert resource_id.startswith(("node/", "interface/", "link/", "k8s/")), (
                resource_id
            )


IntegrationTestCase = IntegrationMixin


class CliIntegrationMixin(IntegrationMixin):
    """Typer CLI runner for tests that exercise command-line behavior."""

    runner: CliRunner

    @pytest.fixture(scope="class")
    def cli_runner(self) -> CliRunner:
        return CliRunner()

    @pytest.fixture(autouse=True)
    def _bind_cli_runner(self, cli_runner: CliRunner) -> None:
        type(self).runner = cli_runner

    def _invoke_ok(self, args: list[str]) -> str:
        result = self.runner.invoke(app, args)
        assert result.exit_code == 0, result.output
        return result.output

    @classmethod
    def _invoke_ok_class(cls, runner: CliRunner, args: list[str]) -> str:
        result = runner.invoke(app, args)
        if result.exit_code != 0:
            raise RuntimeError(
                f"`nika {' '.join(args)}` exited {result.exit_code}:\n{result.output}"
            )
        return result.output


CliIntegrationTestCase = CliIntegrationMixin


class PerTestEnvMixin(IntegrationMixin):
    """Start a fresh lab per test; bind operations to NIKA_SESSION_ID."""

    SCENARIO: ClassVar[str]
    ENV_RUN_ARGS: ClassVar[list[str]] = []

    session_id: str
    _prev_nika_session_id: str | None

    @pytest.fixture(autouse=True)
    def _per_test_env(self):
        self.session_id = self._start_env(self.SCENARIO, self.ENV_RUN_ARGS)
        self._prev_nika_session_id = os.environ.get(SESSION_ID_ENV)
        os.environ[SESSION_ID_ENV] = self.session_id
        try:
            self._assert_session_ready(self.session_id, self.SCENARIO)
            yield
        finally:
            # Close even when setup asserts fail (pytest skips post-yield teardown).
            if getattr(self, "session_id", None):
                self._close_session(self.session_id)
            if getattr(self, "_prev_nika_session_id", None) is None:
                os.environ.pop(SESSION_ID_ENV, None)
            else:
                os.environ[SESSION_ID_ENV] = self._prev_nika_session_id

    def _scenario_kwargs(self, session_id: str | None = None) -> dict:
        return super()._scenario_kwargs(session_id or self.session_id)

    def _problem(self, cls_):
        return super()._problem(cls_, self.session_id)

    @property
    def lab_name(self) -> str:
        return self._session_row(self.session_id)["lab_name"]


PerTestEnvTestCase = PerTestEnvMixin


class SharedSessionMixin(IntegrationMixin):
    """Start one lab for the whole test class; optionally inject a failure up front."""

    SCENARIO: ClassVar[str]
    ENV_RUN_ARGS: ClassVar[list[str]] = []
    INJECT_PROBLEM: ClassVar[str | None] = None
    INJECT_ARGS: ClassVar[list[str]] = []
    INJECT_PARAMS: ClassVar[dict[str, str] | None] = None

    session_id: str

    @pytest.fixture(scope="class", autouse=True)
    def _shared_session(self):
        cls = type(self)
        cls.session_id = cls._start_env_class(cls.SCENARIO, cls.ENV_RUN_ARGS)
        try:
            if cls.INJECT_PROBLEM is not None:
                params = (
                    dict(cls.INJECT_PARAMS)
                    if cls.INJECT_PARAMS
                    else _parse_inject_args(cls.INJECT_ARGS)
                )
                inject_failure_workflow(
                    [cls.INJECT_PROBLEM],
                    session_id=cls.session_id,
                    param_overrides=params or None,
                )
            yield
        finally:
            # Close on inject failure, test errors, and KeyboardInterrupt.
            if getattr(cls, "session_id", None):
                cls._close_session_class(cls.session_id)


SharedSessionTestCase = SharedSessionMixin


class OrderedPipelineMixin(IntegrationMixin):
    """Ordered step tests that share session state across methods in one class."""

    session_id: str | None = None
    session_dir: Path | None = None
    env_destroyed: bool = False

    @pytest.fixture(scope="class", autouse=True)
    def _ordered_pipeline_teardown(self):
        cls = type(self)
        try:
            yield
        finally:
            # Close when step_05 was skipped/failed, or the run was interrupted.
            if cls.session_id and not cls.env_destroyed:
                cls._close_session_class(cls.session_id)
                cls.env_destroyed = True

    def _load_json(self, filename: str) -> dict:
        assert self.session_dir is not None
        return json.loads((self.session_dir / filename).read_text(encoding="utf-8"))

    def _load_jsonl(self, filename: str) -> list[dict]:
        assert self.session_dir is not None
        return [
            json.loads(line)
            for line in (self.session_dir / filename)
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]


OrderedPipelineTestCase = OrderedPipelineMixin
