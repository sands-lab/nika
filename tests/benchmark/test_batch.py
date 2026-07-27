from __future__ import annotations

import pytest
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple
import yaml
from agent.utils.phases import DIAGNOSIS, SUBMISSION
from nika.utils.session_id import resolve_session_tag
from nika.utils.session_store import SESSIONS_DIR, SessionStore
from tests.benchmark.helpers import inject_params_from_benchmark_yaml
from tests.support.integration_base import IntegrationTestCase

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BENCHMARK_DONE_RE = re.compile(
    "benchmark_done session_id=(\\S+) scenario=(\\S+) problem=(\\S+) session_dir=(\\S+)"
)


class ScenarioCase(NamedTuple):
    scenario: str
    problem: str
    size: str | None = None


SCENARIO_CASES: list[ScenarioCase] = [
    # Keep the parallel mock matrix on lightweight Kathara labs. Heavier multi-topo
    # rows (ospf/dc_clos) flake under concurrent Docker pressure with ToolException
    # on ping_pair / get_reachability; sandbox parallel coverage lives in
    # test_sandbox_benchmark.py instead.
    ScenarioCase("simple_bgp", "link_down"),
    ScenarioCase("simple_bgp", "link_flap"),
    ScenarioCase("simple_bgp", "link_detach"),
]


def _case_key(case: ScenarioCase) -> str:
    return f"{case.scenario}:{case.problem}"


class ParallelBenchmarkIntegrationTest(IntegrationTestCase):
    """Run all benchmark YAML rows as one parallel batch, then verify per-session results."""

    _pipeline_results: dict[str, tuple[str, Path] | BaseException]
    _result_root: Path

    @pytest.fixture(scope="class", autouse=True)
    def _setup_class(self) -> None:
        type(self)._pipeline_results = {}
        result_root = Path(tempfile.mkdtemp(prefix="nika-batch-"))
        type(self)._result_root = result_root
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            cases = []
            for case in SCENARIO_CASES:
                inject = inject_params_from_benchmark_yaml(
                    case.scenario, case.problem, case.size or ""
                )
                # size=s curated YAML picks super_spine for blackhole leaks, but that
                # device has no client host for resolve_victim_host().
                if (
                    case.problem == "bgp_blackhole_route_leak"
                    and "super_spine" in inject.get("host_name", "")
                ):
                    inject["host_name"] = "leaf_router_0_0"
                row = {
                    "scenario": case.scenario,
                    "problem": case.problem,
                    "topo_size": case.size,
                    "inject": inject,
                }
                cases.append(row)
            yaml.dump({"cases": cases}, handle, sort_keys=False, allow_unicode=True)
            yaml_path = handle.name
        try:
            proc = subprocess.run(
                [
                    "uv",
                    "run",
                    "nika",
                    "benchmark",
                    "run",
                    "--config",
                    yaml_path,
                    "--batch-size",
                    "3",
                    "--agent",
                    "mock",
                    "--model",
                    "mock-v1",
                    "-n",
                    "5",
                    "--result_dir",
                    str(result_root),
                    "--session-tag",
                    resolve_session_tag(context="test"),
                ],
                cwd=_REPO_ROOT,
                capture_output=True,
                text=True,
            )
            output = proc.stdout
            if proc.stderr:
                output += proc.stderr
            if proc.returncode != 0:
                raise RuntimeError(
                    f"`nika benchmark run --batch-size 3` exited {proc.returncode}:\n{output}"
                )
            parsed: dict[str, tuple[str, Path]] = {}
            for match in _BENCHMARK_DONE_RE.finditer(output):
                session_id, scenario, problem, session_dir = match.groups()
                parsed[f"{scenario}:{problem}"] = (session_id, Path(session_dir))
            results: dict[str, tuple[str, Path] | BaseException] = {}
            for case in SCENARIO_CASES:
                key = _case_key(case)
                if key not in parsed:
                    results[key] = RuntimeError(
                        f"benchmark_done line missing for {key} in output:\n{output}"
                    )
                else:
                    results[key] = parsed[key]
            type(self)._pipeline_results = results
        finally:
            Path(yaml_path).unlink(missing_ok=True)

    def _result(self, case: ScenarioCase) -> tuple[str, Path]:
        result = type(self)._pipeline_results.get(_case_key(case))
        if isinstance(result, BaseException):
            raise AssertionError(f"Pipeline for {_case_key(case)} raised: {result}")

        assert result is not None
        return result

    def _load_json(self, session_dir: Path, filename: str) -> dict:
        path = session_dir / filename

        assert path.exists(), f"{filename} missing in {session_dir}"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_session_ids_are_unique(self) -> None:
        ids = [
            self._pipeline_results[_case_key(c)][0]
            for c in SCENARIO_CASES
            if not isinstance(self._pipeline_results.get(_case_key(c)), BaseException)
        ]

        assert len(ids) == len(set(ids)), f"Duplicate session IDs: {ids}"
        for session_id in ids:
            # Batch --config uses trial ids: {case_key}__t01
            assert session_id.endswith("__t01"), session_id

    def test_session_dirs_are_isolated(self) -> None:
        dirs = [
            str(self._pipeline_results[_case_key(c)][1])
            for c in SCENARIO_CASES
            if not isinstance(self._pipeline_results.get(_case_key(c)), BaseException)
        ]

        assert len(dirs) == len(set(dirs)), f"Overlapping session dirs: {dirs}"
        for path in dirs:
            assert "/trials/" in path.replace("\\", "/")

    def test_ground_truth_correctness(self) -> None:
        for case in SCENARIO_CASES:
            _, session_dir = self._result(case)
            gt = self._load_json(session_dir, "ground_truth.json")

            assert gt["is_anomaly"]

            assert case.problem in gt["root_cause_name"]

    def test_run_json_correctness(self) -> None:
        for case in SCENARIO_CASES:
            session_id, session_dir = self._result(case)
            run = self._load_json(session_dir, "run.json")

            assert run["session_id"] == session_id

            assert run["scenario_name"] == case.scenario

            assert run["agent_type"] == "mock"

            assert run["status"] == "finished"

    def test_session_dir_path_contains_session_id(self) -> None:
        for case in SCENARIO_CASES:
            session_id, session_dir = self._result(case)

            assert session_id in str(session_dir)

    def test_submission_fields_and_isolation(self) -> None:
        for case in SCENARIO_CASES:
            session_id, session_dir = self._result(case)
            sub = self._load_json(session_dir, "submission.json")
            for field in ("is_anomaly", "faulty_devices", "root_cause_name"):
                assert field in sub, f"Missing field '{field}' in submission.json"

            assert session_id in str(session_dir)

    def test_eval_metrics_fields_and_scores(self) -> None:
        required_fields = (
            "detection_score",
            "localization_accuracy",
            "localization_f1",
            "rca_accuracy",
            "rca_f1",
            "tool_calls",
        )
        for case in SCENARIO_CASES:
            _, session_dir = self._result(case)
            metrics = self._load_json(session_dir, "eval_metrics.json")
            for field in required_fields:
                assert field in metrics, f"Missing field '{field}' in eval_metrics.json"

            assert metrics["detection_score"] == 1.0

            assert metrics["rca_accuracy"] == 1.0

            assert metrics["tool_calls"] > 0

            assert not (session_dir / "llm_judge.json").exists()

    def test_eval_summary_aggregates_batch(self) -> None:
        """Summary is a post-hoc ``nika eval`` step; benchmark does not write judge/summary."""
        from nika.workflows.eval.summary import run_eval_summary

        out = run_eval_summary(results_dir=str(type(self)._result_root))
        assert out.is_file()
        text = out.read_text(encoding="utf-8")
        for case in SCENARIO_CASES:
            session_id, session_dir = self._result(case)
            assert session_id in text
            assert not (session_dir / "llm_judge.json").exists()

    def test_messages_trace_has_expected_tool_calls(self) -> None:
        for case in SCENARIO_CASES:
            _, session_dir = self._result(case)
            trace_path = session_dir / "messages.jsonl"

            assert trace_path.exists(), "messages.jsonl missing"
            events = [
                json.loads(line)
                for line in trace_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            agents_seen = {e["agent"] for e in events}

            assert DIAGNOSIS in agents_seen

            assert SUBMISSION in agents_seen
            tool_names_seen = {
                e["tool"]["name"]
                for e in events
                if e.get("event") == "tool_start" and "tool" in e
            }

            assert "list_avail_problems" in tool_names_seen

            assert "submit" in tool_names_seen

    def test_runtime_session_files_cleared_after_close(self) -> None:
        for case in SCENARIO_CASES:
            session_id, _ = self._result(case)
            runtime_path = Path(SESSIONS_DIR) / f"{session_id}.json"

            assert not runtime_path.exists(), (
                f"Runtime session file was not removed after undeploy: {runtime_path}"
            )
            with pytest.raises(FileNotFoundError):
                SessionStore().get_session(session_id)
