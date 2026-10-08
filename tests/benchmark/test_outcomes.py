"""Tests for counted vs retryable trial failure outcomes."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from nika.workflows.benchmark.outcomes import (
    case_timeout_endpoint_dominated,
    classify_trial_failure,
    is_endpoint_exception,
)
from nika.workflows.benchmark.trials import (
    expand_trials,
    heal_trial_outcome,
    is_finalized_failure,
    is_valid_trial,
    scan_trials,
    trial_dir,
)
from tests.benchmark.trial_helpers import ROW_A

pytestmark = pytest.mark.unit


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )


def _write_agent_start(session_dir: Path, ts: datetime) -> None:
    _write_jsonl(
        session_dir / "nika.jsonl",
        [{"timestamp": ts.isoformat(), "event": "agent_start", "message": "start"}],
    )


class TestClassifyTrialFailure:
    def test_missing_submission_is_agent_failed(self) -> None:
        err = RuntimeError(
            "Agent completed without writing required submission: /tmp/x/submission.json"
        )
        assert classify_trial_failure(err) == "agent_failed"

    def test_case_timeout_is_agent_failed_without_session(self) -> None:
        err = RuntimeError(
            "[t01] case exceeded --case-timeout (2400s) and was killed. "
            "Its lab may be leaked — check `nika session ps`."
        )
        assert classify_trial_failure(err) == "agent_failed"

    def test_case_timeout_tool_heavy_stays_agent_failed(self, tmp_path: Path) -> None:
        t0 = datetime(2026, 9, 23, 22, 0, 0, tzinfo=UTC)
        _write_agent_start(tmp_path, t0)
        _write_jsonl(
            tmp_path / "messages.jsonl",
            [
                {
                    "timestamp": t0.isoformat(),
                    "event": "llm_start",
                    "phase": "diagnosis",
                },
                {
                    "timestamp": (t0 + timedelta(seconds=30)).isoformat(),
                    "event": "llm_end",
                    "phase": "diagnosis",
                },
                {
                    "timestamp": (t0 + timedelta(seconds=40)).isoformat(),
                    "event": "tool_start",
                    "phase": "diagnosis",
                },
                {
                    "timestamp": (t0 + timedelta(seconds=2000)).isoformat(),
                    "event": "tool_end",
                    "phase": "diagnosis",
                },
            ],
        )
        err = RuntimeError("[t01] case exceeded --case-timeout (2400s) and was killed.")
        until = t0 + timedelta(seconds=2000)
        assert not case_timeout_endpoint_dominated(tmp_path, until=until)
        assert (
            classify_trial_failure(err, session_dir=tmp_path, until=until)
            == "agent_failed"
        )

    def test_case_timeout_llm_dominated_is_endpoint_failed(
        self, tmp_path: Path
    ) -> None:
        t0 = datetime(2026, 9, 23, 22, 0, 0, tzinfo=UTC)
        _write_jsonl(
            tmp_path / "nika.jsonl",
            [
                {
                    "timestamp": t0.isoformat(),
                    "event": "agent_start",
                    "message": "start",
                }
            ],
        )
        _write_jsonl(
            tmp_path / "messages.jsonl",
            [
                {
                    "timestamp": (t0 + timedelta(seconds=1)).isoformat(),
                    "event": "llm_start",
                    "phase": "diagnosis",
                },
                {
                    "timestamp": (t0 + timedelta(seconds=1800)).isoformat(),
                    "event": "llm_end",
                    "phase": "diagnosis",
                },
                {
                    "timestamp": (t0 + timedelta(seconds=1801)).isoformat(),
                    "event": "llm_start",
                    "phase": "diagnosis",
                },
            ],
        )
        until = t0 + timedelta(seconds=2400)
        err = RuntimeError("[t01] case exceeded --case-timeout (2400s) and was killed.")
        assert case_timeout_endpoint_dominated(tmp_path, until=until)
        assert (
            classify_trial_failure(err, session_dir=tmp_path, until=until)
            == "endpoint_failed"
        )

    def test_case_timeout_llm_heavy_but_ended_in_tool_is_agent_failed(
        self, tmp_path: Path
    ) -> None:
        """Long LLM calls then kill during a tool → still counted agent_failed."""
        t0 = datetime(2026, 9, 23, 22, 0, 0, tzinfo=UTC)
        _write_agent_start(tmp_path, t0)
        _write_jsonl(
            tmp_path / "messages.jsonl",
            [
                {
                    "timestamp": t0.isoformat(),
                    "event": "llm_start",
                    "phase": "diagnosis",
                },
                {
                    "timestamp": (t0 + timedelta(seconds=2000)).isoformat(),
                    "event": "llm_end",
                    "phase": "diagnosis",
                },
                {
                    "timestamp": (t0 + timedelta(seconds=2001)).isoformat(),
                    "event": "tool_start",
                    "phase": "diagnosis",
                },
            ],
        )
        until = t0 + timedelta(seconds=2400)
        err = RuntimeError("[t01] case exceeded --case-timeout (2400s) and was killed.")
        assert not case_timeout_endpoint_dominated(tmp_path, until=until)
        assert (
            classify_trial_failure(err, session_dir=tmp_path, until=until)
            == "agent_failed"
        )

    def test_case_timeout_llm_below_fraction_stays_agent_failed(
        self, tmp_path: Path
    ) -> None:
        """Mid-LLM kill with only ~60% LLM wall time stays agent_failed."""
        t0 = datetime(2026, 9, 23, 22, 0, 0, tzinfo=UTC)
        _write_agent_start(tmp_path, t0)
        _write_jsonl(
            tmp_path / "messages.jsonl",
            [
                {
                    "timestamp": t0.isoformat(),
                    "event": "tool_start",
                    "phase": "diagnosis",
                },
                {
                    "timestamp": (t0 + timedelta(seconds=1000)).isoformat(),
                    "event": "tool_end",
                    "phase": "diagnosis",
                },
                {
                    "timestamp": (t0 + timedelta(seconds=1000)).isoformat(),
                    "event": "llm_start",
                    "phase": "diagnosis",
                },
            ],
        )
        until = t0 + timedelta(seconds=2500)  # LLM open 1500/2500 = 0.6 < 0.8
        err = RuntimeError("[t01] case exceeded --case-timeout (2500s) and was killed.")
        assert not case_timeout_endpoint_dominated(tmp_path, until=until)
        assert (
            classify_trial_failure(err, session_dir=tmp_path, until=until)
            == "agent_failed"
        )

    def test_connection_error_is_endpoint_failed(self) -> None:
        assert classify_trial_failure(ConnectionError("Connection refused")) == (
            "endpoint_failed"
        )

    def test_taskgroup_with_nested_connect_is_endpoint_failed(self) -> None:
        nested = ConnectionError("All connection attempts failed")
        group = ExceptionGroup(
            "unhandled errors in a TaskGroup (1 sub-exception)", [nested]
        )
        assert is_endpoint_exception(group)
        assert classify_trial_failure(group) == "endpoint_failed"

    def test_generic_agent_crash_stays_agent_failed(self) -> None:
        assert classify_trial_failure(RuntimeError("agent boom")) == "agent_failed"

    def test_http_502_message_is_endpoint_failed(self) -> None:
        err = RuntimeError("Error code: 502 — Bad Gateway from vLLM endpoint")
        assert classify_trial_failure(err) == "endpoint_failed"

    def test_failure_before_agent_started_is_infra_failed(self, tmp_path: Path) -> None:
        """sbx/gateway/store failures after GT but before any agent turn."""
        err = RuntimeError(
            "NIKA_MCP_GATEWAY_AGENT_URL was not set for sandbox execution"
        )
        assert classify_trial_failure(err, session_dir=tmp_path) == "infra_failed"
        # agent_start alone (no model/tool event) is still infrastructure.
        t0 = datetime(2026, 9, 23, 22, 0, 0, tzinfo=UTC)
        _write_agent_start(tmp_path, t0)
        _write_jsonl(
            tmp_path / "messages.jsonl",
            [{"timestamp": t0.isoformat(), "event": "subprocess_start"}],
        )
        assert classify_trial_failure(err, session_dir=tmp_path) == "infra_failed"
        timeout = RuntimeError("[t01] case exceeded --case-timeout (2400s)")
        assert classify_trial_failure(timeout, session_dir=tmp_path) == "infra_failed"
        refused = ConnectionError("Connection refused")
        assert classify_trial_failure(refused, session_dir=tmp_path) == (
            "endpoint_failed"
        )

    def test_failure_after_agent_turn_is_agent_failed(self, tmp_path: Path) -> None:
        t0 = datetime(2026, 9, 23, 22, 0, 0, tzinfo=UTC)
        _write_agent_start(tmp_path, t0)
        # Claude CLI stream events count as agent activity (no llm_start).
        _write_jsonl(
            tmp_path / "messages.jsonl",
            [{"timestamp": t0.isoformat(), "event": "assistant"}],
        )
        err = RuntimeError("agent boom")
        assert classify_trial_failure(err, session_dir=tmp_path) == "agent_failed"

    @pytest.mark.parametrize(
        "err",
        [
            RuntimeError("[t01] trial worker exited with code -15"),
            RuntimeError("[t01] trial worker exited with code 143"),
            SystemExit(143),
        ],
    )
    def test_signal_kill_mid_llm_is_endpoint_failed(
        self, tmp_path: Path, err: BaseException
    ) -> None:
        t0 = datetime(2026, 9, 23, 22, 0, 0, tzinfo=UTC)
        _write_agent_start(tmp_path, t0)
        in_llm = [
            {"timestamp": t0.isoformat(), "event": "llm_start"},
            {"timestamp": (t0 + timedelta(seconds=5)).isoformat(), "event": "llm_end"},
            {
                "timestamp": (t0 + timedelta(seconds=6)).isoformat(),
                "event": "tool_start",
            },
            {"timestamp": (t0 + timedelta(seconds=7)).isoformat(), "event": "tool_end"},
            {
                "timestamp": (t0 + timedelta(seconds=8)).isoformat(),
                "event": "llm_start",
            },
        ]
        _write_jsonl(tmp_path / "messages.jsonl", in_llm)
        assert classify_trial_failure(err, session_dir=tmp_path) == "endpoint_failed"
        # Killed during a tool call: the model answered, so it stays counted.
        _write_jsonl(tmp_path / "messages.jsonl", in_llm[:3])
        assert classify_trial_failure(err, session_dir=tmp_path) == "agent_failed"

    def test_nonsignal_crash_mid_llm_stays_agent_failed(self, tmp_path: Path) -> None:
        t0 = datetime(2026, 9, 23, 22, 0, 0, tzinfo=UTC)
        _write_agent_start(tmp_path, t0)
        _write_jsonl(
            tmp_path / "messages.jsonl",
            [{"timestamp": t0.isoformat(), "event": "llm_start"}],
        )
        err = RuntimeError("[t01] trial worker exited with code 1")
        assert classify_trial_failure(err, session_dir=tmp_path) == "agent_failed"

    def test_codex_stall_waiting_on_model_is_endpoint_failed(
        self, tmp_path: Path
    ) -> None:
        t0 = datetime(2026, 9, 23, 22, 0, 0, tzinfo=UTC)
        _write_agent_start(tmp_path, t0)
        diagnosis = [
            {"event": "thread.started"},
            {"event": "tool_start"},
            {"event": "tool_end"},
        ]
        stall = {
            "event": "subprocess_stall",
            "stall_s": 300,
            "reconnect_failure": False,
        }
        err = RuntimeError(
            "Agent completed without writing required submission: /tmp/x/submission.json"
        )
        # A failed tool call closes the tool; the stall that follows is the model's.
        _write_jsonl(
            tmp_path / "messages.jsonl",
            [*diagnosis, {"event": "tool_start"}, {"event": "tool_error"}, stall],
        )
        assert classify_trial_failure(err, session_dir=tmp_path) == "endpoint_failed"
        # Submission phase stalled before the model answered.
        _write_jsonl(
            tmp_path / "messages.jsonl",
            [*diagnosis, {"event": "thread.started"}, stall],
        )
        assert classify_trial_failure(err, session_dir=tmp_path) == "endpoint_failed"
        # Stalled inside a tool call: the tool hung, not the model.
        _write_jsonl(
            tmp_path / "messages.jsonl", [*diagnosis, {"event": "tool_start"}, stall]
        )
        assert classify_trial_failure(err, session_dir=tmp_path) == "agent_failed"
        # Reconnect failures are transport errors even mid-tool.
        _write_jsonl(
            tmp_path / "messages.jsonl",
            [*diagnosis, {"event": "tool_start"}, dict(stall, reconnect_failure=True)],
        )
        assert classify_trial_failure(err, session_dir=tmp_path) == "endpoint_failed"

    def test_codex_turn_failed_on_endpoint_is_endpoint_failed(
        self, tmp_path: Path
    ) -> None:
        t0 = datetime(2026, 9, 23, 22, 0, 0, tzinfo=UTC)
        _write_agent_start(tmp_path, t0)

        def turn_failed(message: str) -> dict:
            event = {"type": "turn.failed", "error": {"message": message}}
            return {"event": "turn.failed", "codex_event": event}

        err = RuntimeError("ERROR: diagnosis phase exited with code 1.")
        unavailable = "unexpected status 503 Service Unavailable: No healthy backend"
        _write_jsonl(
            tmp_path / "messages.jsonl",
            [{"event": "tool_start"}, {"event": "tool_end"}, turn_failed(unavailable)],
        )
        assert classify_trial_failure(err, session_dir=tmp_path) == "endpoint_failed"
        # A request the endpoint rejected is the agent's problem.
        _write_jsonl(
            tmp_path / "messages.jsonl",
            [{"event": "tool_start"}, turn_failed("unexpected status 400 Bad Request")],
        )
        assert classify_trial_failure(err, session_dir=tmp_path) == "agent_failed"

    def test_claude_api_error_result_is_endpoint_failed(self, tmp_path: Path) -> None:
        t0 = datetime(2026, 9, 23, 22, 0, 0, tzinfo=UTC)
        _write_agent_start(tmp_path, t0)

        def result(status: int | None) -> dict:
            event = {
                "subtype": "success",
                "is_error": True,
                "terminal_reason": "api_error",
                "api_error_status": status,
                "result": "API Error: The response stopped arriving.",
            }
            return {"event": "result", "claude_event": event}

        reply = {"event": "assistant", "claude_event": {"message": {"model": "m"}}}
        err = RuntimeError("ERROR: diagnosis phase exited with code 1.")
        # The stream stopped mid-response (no HTTP status).
        _write_jsonl(tmp_path / "messages.jsonl", [reply, result(None)])
        assert classify_trial_failure(err, session_dir=tmp_path) == "endpoint_failed"
        # A request the API rejected stays the agent's failure.
        _write_jsonl(tmp_path / "messages.jsonl", [reply, result(400)])
        assert classify_trial_failure(err, session_dir=tmp_path) == "agent_failed"

    def test_claude_timeout_mid_model_call_is_endpoint_failed(
        self, tmp_path: Path
    ) -> None:
        t0 = datetime(2026, 9, 23, 22, 0, 0, tzinfo=UTC)
        _write_agent_start(tmp_path, t0)

        def at(minutes: float, event: str, *, tool: bool = False) -> dict:
            row = {"timestamp": (t0 + timedelta(minutes=minutes)).isoformat()}
            if event == "prompt":
                return {**row, "event": "prompt"}
            if event == "user":
                return {**row, "event": "user", "claude_event": {"type": "user"}}
            content = [{"type": "tool_use" if tool else "thinking"}]
            message = {"model": "m", "content": content}
            return {**row, "event": "assistant", "claude_event": {"message": message}}

        # Claude Code logs no llm_start/llm_end: model calls run from the prompt
        # or a tool result to the block that requests the next tool.
        turns = [
            at(0, "prompt"),
            at(50, "assistant", tool=True),
            at(51, "user"),
            at(100, "assistant", tool=True),
            at(101, "user"),
        ]
        err = RuntimeError("agent run exceeded agent.timeout_sec (7200s)")
        kill = t0 + timedelta(minutes=120)
        # Killed while waiting for the model after a tool result.
        _write_jsonl(tmp_path / "messages.jsonl", turns)
        assert (
            classify_trial_failure(err, session_dir=tmp_path, until=kill)
            == "endpoint_failed"
        )
        # Streaming thinking, no tool requested yet: still inside the call.
        _write_jsonl(tmp_path / "messages.jsonl", [*turns, at(110, "assistant")])
        assert (
            classify_trial_failure(err, session_dir=tmp_path, until=kill)
            == "endpoint_failed"
        )
        # Killed while the requested tool runs.
        _write_jsonl(
            tmp_path / "messages.jsonl", [*turns, at(110, "assistant", tool=True)]
        )
        assert (
            classify_trial_failure(err, session_dir=tmp_path, until=kill)
            == "agent_failed"
        )
        # Tool-heavy run: model calls are a small share of the budget.
        tool_heavy = [
            at(0, "prompt"),
            at(5, "assistant", tool=True),
            at(100, "user"),
        ]
        _write_jsonl(tmp_path / "messages.jsonl", tool_heavy)
        assert (
            classify_trial_failure(err, session_dir=tmp_path, until=kill)
            == "agent_failed"
        )

    def test_claude_retries_exhausted_is_endpoint_failed(self, tmp_path: Path) -> None:
        t0 = datetime(2026, 9, 23, 22, 0, 0, tzinfo=UTC)
        _write_agent_start(tmp_path, t0)

        def retry(attempt: int, status: int = 503) -> dict:
            event = {
                "subtype": "api_retry",
                "attempt": attempt,
                "max_retries": 10,
                "error_status": status,
            }
            return {"event": "system", "claude_event": event}

        def reply(model: str) -> dict:
            return {"event": "assistant", "claude_event": {"message": {"model": model}}}

        err = RuntimeError("ERROR: diagnosis phase exited with code 1.")
        gave_up = [reply("Qwen3.8-27B-FP8"), retry(9), retry(10), reply("<synthetic>")]
        _write_jsonl(tmp_path / "messages.jsonl", gave_up)
        assert classify_trial_failure(err, session_dir=tmp_path) == "endpoint_failed"
        # The model answered again after the retries ran out.
        _write_jsonl(tmp_path / "messages.jsonl", [*gave_up, reply("Qwen3.8-27B-FP8")])
        assert classify_trial_failure(err, session_dir=tmp_path) == "agent_failed"
        # Retries still left, or a non-retryable status: not an endpoint give-up.
        for events in ([retry(9)], [retry(10, status=400)]):
            _write_jsonl(
                tmp_path / "messages.jsonl", [reply("Qwen3.8-27B-FP8"), *events]
            )
            assert classify_trial_failure(err, session_dir=tmp_path) == "agent_failed"


@pytest.mark.parametrize("retryable", ["endpoint_failed", "infra_failed"])
class TestRetryableResume:
    def test_retryable_is_not_counted(self, tmp_path: Path, retryable: str) -> None:
        path = tmp_path / "trial"
        path.mkdir()
        (path / "run.json").write_text(
            json.dumps(
                {
                    "session_id": "t",
                    "status": "error",
                    "outcome": retryable,
                    "agent_error": "Connection refused",
                }
            ),
            encoding="utf-8",
        )
        (path / "ground_truth.json").write_text("{}", encoding="utf-8")
        (path / "messages.jsonl").write_text("", encoding="utf-8")
        assert not is_valid_trial(path)
        assert is_finalized_failure(path)
        assert heal_trial_outcome(path) is False

    def test_scan_resume_retries_retryable(
        self, tmp_path: Path, retryable: str
    ) -> None:
        trials = expand_trials([ROW_A], n_trials=1)
        trial = trials[0]
        path = trial_dir(tmp_path, trial.case_key, trial.trial_index)
        path.mkdir(parents=True)
        (path / "run.json").write_text(
            json.dumps(
                {
                    "session_id": trial.trial_id,
                    "status": "error",
                    "outcome": retryable,
                    "agent_error": "Connection refused",
                }
            ),
            encoding="utf-8",
        )
        (path / "ground_truth.json").write_text("{}", encoding="utf-8")
        (path / "messages.jsonl").write_text("", encoding="utf-8")

        with patch(
            "nika.workflows.benchmark.trials.cleanup_benchmark_session"
        ) as cleanup:
            _, pending = scan_trials(
                trials=trials,
                result_dir=tmp_path,
                resume=True,
                mutate=True,
                announce=False,
            )
        assert pending == [0]
        cleanup.assert_called_once()
        # cleanup_benchmark_session owns deletion; we only assert it was invoked.


def _run_case_with_failing_agent(tmp_path: Path, agent_effect) -> Path:
    """Run one batch trial with fake deploy/inject; ``agent_effect`` is the agent."""
    result_dir = tmp_path / "run"
    trials = expand_trials([ROW_A], n_trials=1)
    trial = trials[0]
    session_path = trial_dir(result_dir, trial.case_key, 1)
    created: list[str] = []

    def fake_start_net_env(*args, **kwargs):
        sid = kwargs["session_id"]
        created.append(sid)
        sdir = Path(kwargs["session_dir"])
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "run.json").write_text(
            json.dumps(
                {
                    "session_id": sid,
                    "status": "running",
                    "session_dir": str(sdir),
                    "scenario_name": "dc_clos",
                }
            ),
            encoding="utf-8",
        )
        from nika.utils.session_store import SessionStore

        SessionStore().create_session(
            {
                "session_id": sid,
                "lab_name": "lab",
                "scenario_name": "dc_clos",
                "scenario_topo_size": None,
                "scenario_params": {},
                "session_dir": str(sdir),
                "status": "running",
                "backend": "kathara",
            }
        )
        return sid

    def fake_inject(**kwargs):
        (session_path / "ground_truth.json").write_text(
            json.dumps(
                {
                    "is_anomaly": True,
                    "root_causes": [
                        {"resource_id": "node/pc1", "fault_type": "link_down"}
                    ],
                    "failure_domain": "link_interface",
                }
            ),
            encoding="utf-8",
        )

    def fake_agent(**kwargs):
        agent_effect(session_path)

    with (
        patch(
            "nika.workflows.benchmark.run.start_net_env",
            side_effect=fake_start_net_env,
        ),
        patch("nika.workflows.benchmark.run.inject_failure", side_effect=fake_inject),
        patch("nika.workflows.benchmark.run.start_agent", side_effect=fake_agent),
        patch("nika.workflows.benchmark.run.close_session"),
        patch("nika.workflows.benchmark.run._stamp_release_meta"),
        patch("nika.workflows.benchmark.run._stamp_trial_meta"),
        patch(
            "nika.workflows.benchmark.run.Session.load_running_session",
            side_effect=lambda *a, **k: type(
                "S",
                (),
                {"update_session": lambda self, key, value: None},
            )(),
        ),
    ):
        from nika.utils.session_store import SessionStore
        from nika.workflows.benchmark.run import run_single_case

        try:
            run_single_case(
                problem="link_down",
                scenario="dc_clos",
                topo_size=str(ROW_A.get("topo_size") or "s"),
                agent_type="mock",
                llm_provider=None,
                model="mock-v1",
                max_steps=None,
                inject_params=ROW_A["inject"],
                result_dir=str(result_dir),
                trial_id=trial.trial_id,
                trial_index=trial.trial_index,
                case_key=trial.case_key,
            )
        finally:
            # close_session is mocked, so drop the fake session record here.
            for sid in created:
                SessionStore().delete_session(sid)
    return session_path


class TestPostInjectFinalize:
    @pytest.mark.parametrize(
        ("error", "outcome"),
        [
            (ConnectionError("Connection refused"), "endpoint_failed"),
            (
                RuntimeError("Docker Sandboxes CLI (sbx) is not available."),
                "infra_failed",
            ),
        ],
    )
    def test_failure_before_agent_turn_is_retryable(
        self, tmp_path: Path, error: Exception, outcome: str
    ) -> None:
        def agent(_session_path: Path) -> None:
            raise error

        with pytest.raises(type(error)):
            _run_case_with_failing_agent(tmp_path, agent)
        session_path = next((tmp_path / "run" / "trials").iterdir())
        assert not is_valid_trial(session_path)
        assert is_finalized_failure(session_path)
        run_meta = json.loads((session_path / "run.json").read_text(encoding="utf-8"))
        assert run_meta["outcome"] == outcome
        assert run_meta["status"] == "error"
        assert not (session_path / "eval_metrics.json").exists()

    def test_valid_submission_then_timeout_is_success(self, tmp_path: Path) -> None:
        """Worker and resume heal agree: a submission means the agent finished."""

        def agent(session_path: Path) -> None:
            (session_path / "submission.json").write_text(
                json.dumps({"is_anomaly": True, "root_causes": []}), encoding="utf-8"
            )
            raise RuntimeError("agent run exceeded agent.timeout_sec (1800s)")

        session_path = _run_case_with_failing_agent(tmp_path, agent)
        assert is_valid_trial(session_path)
        run_meta = json.loads((session_path / "run.json").read_text(encoding="utf-8"))
        assert run_meta["outcome"] == "success"
        assert "timeout_sec" in run_meta["agent_error"]
        assert run_meta["end_time"]
