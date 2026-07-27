from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

from nika.utils.session_id import resolve_session_tag
from tests.agent.sandbox_support import (
    sandbox_anthropic_credential_available,
    sandbox_openai_credential_available,
    sandbox_runtime_available,
)
from tests.benchmark.helpers import inject_params_from_benchmark_yaml
from tests.support.integration_pipeline import (
    claude_cli_available,
    codex_cli_available,
    load_test_env,
)

load_test_env()

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BENCHMARK_DONE_RE = re.compile(
    r"benchmark_done session_id=(\S+) scenario=(\S+) problem=(\S+) session_dir=(\S+)"
)
_SENSITIVE_NAMES = ("auth.json", ".credentials.json", ".host_auth")

_PARALLEL_CASES = (
    ("simple_bgp", "link_down"),
    ("simple_bgp", "link_flap"),
)

CLAUDE_MODEL = "deepseek-v4-flash"
CODEX_MODEL = "gpt-5-mini"


def _assert_sandbox_session(session_path: Path, *, agent_type: str) -> None:
    manifest = session_path / "sandbox_manifest.json"
    assert manifest.is_file(), f"missing sandbox manifest under {session_path}"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["agent_type"] == agent_type

    manifest_text = manifest.read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY" not in manifest_text
    assert "DEEPSEEK_API_KEY" not in manifest_text
    assert "OPENAI_API_KEY" not in manifest_text
    assert not (session_path / ".sandbox_run").exists()
    for dirname in (
        "codex_workspace",
        "claude_workspace",
        "codex_sdk_workspace",
        "claude_sdk_workspace",
    ):
        assert not (session_path / dirname).exists()

    for path in session_path.rglob("*"):
        if path.name in _SENSITIVE_NAMES:
            pytest.fail(f"credential file leaked into results: {path}")

    assert (session_path / "messages.jsonl").is_file()
    assert (session_path / "submission.json").is_file()
    assert (session_path / "eval_metrics.json").is_file()

    metrics = json.loads((session_path / "eval_metrics.json").read_text())
    for field in (
        "detection_score",
        "localization_accuracy",
        "rca_accuracy",
        "tool_calls",
    ):
        assert field in metrics


def _run_parallel_sandbox_benchmark(
    *,
    agent_type: str,
    model: str,
) -> None:
    """Two concurrent sandbox sessions must stay isolated and produce artifacts."""
    cases = []
    for scenario, problem in _PARALLEL_CASES:
        cases.append(
            {
                "scenario": scenario,
                "problem": problem,
                "topo_size": "",
                "inject": inject_params_from_benchmark_yaml(scenario, problem, ""),
            }
        )

    result_root = Path(tempfile.mkdtemp(prefix="nika-sbx-bench-"))
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as handle:
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
                "2",
                "-a",
                agent_type,
                "-m",
                model,
                "-n",
                "10",
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
        assert proc.returncode == 0, output

        parsed: dict[str, Path] = {}
        for match in _BENCHMARK_DONE_RE.finditer(output):
            _session_id, scenario, problem, session_dir = match.groups()
            parsed[f"{scenario}:{problem}"] = Path(session_dir)

        assert len(parsed) == len(_PARALLEL_CASES), (
            f"expected {len(_PARALLEL_CASES)} benchmark_done lines, got "
            f"{len(parsed)}:\n{output}"
        )
        dirs = list(parsed.values())
        assert len(dirs) == len(set(dirs)), f"overlapping session dirs: {dirs}"

        for scenario, problem in _PARALLEL_CASES:
            key = f"{scenario}:{problem}"
            assert key in parsed, f"missing {key} in output:\n{output}"
            _assert_sandbox_session(parsed[key], agent_type=agent_type)
    finally:
        Path(yaml_path).unlink(missing_ok=True)


@pytest.mark.skipif(
    not sandbox_runtime_available(),
    reason="Docker Sandboxes runtime not available",
)
@pytest.mark.skipif(
    not (claude_cli_available() and sandbox_anthropic_credential_available()),
    reason="Claude CLI + DeepSeek/anthropic sbx credentials required",
)
class SandboxBenchmarkClaudeTest:
    """Claude sandbox path for ``nika benchmark run`` (DeepSeek API)."""

    def test_single_case_produces_sandbox_artifacts(self) -> None:
        proc = subprocess.run(
            [
                "uv",
                "run",
                "nika",
                "benchmark",
                "run",
                "simple_bgp",
                "--problem",
                "link_down",
                "--set",
                "host_name=pc1",
                "--set",
                "intf_name=eth0",
                "-a",
                "local_cli.claude_cli",
                "-m",
                CLAUDE_MODEL,
                "-n",
                "10",
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
        assert proc.returncode == 0, output

        match = _BENCHMARK_DONE_RE.search(output)
        assert match is not None, f"benchmark_done line missing in output:\n{output}"
        _session_id, scenario, problem, session_dir = match.groups()
        assert scenario == "simple_bgp"
        assert problem == "link_down"
        _assert_sandbox_session(Path(session_dir), agent_type="local_cli.claude_cli")

    def test_parallel_batch_produces_sandbox_artifacts(self) -> None:
        _run_parallel_sandbox_benchmark(
            agent_type="local_cli.claude_cli",
            model=CLAUDE_MODEL,
        )


@pytest.mark.skipif(
    not sandbox_runtime_available(),
    reason="Docker Sandboxes runtime not available",
)
@pytest.mark.skipif(
    not (codex_cli_available() and sandbox_openai_credential_available()),
    reason="Codex CLI + OPENAI_API_KEY / openai sbx credentials required",
)
class SandboxBenchmarkCodexTest:
    """Codex sandbox path for ``nika benchmark run`` (OpenAI API)."""

    def test_parallel_batch_produces_sandbox_artifacts(self) -> None:
        _run_parallel_sandbox_benchmark(
            agent_type="local_cli.codex_cli",
            model=CODEX_MODEL,
        )
