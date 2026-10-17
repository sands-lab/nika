from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from nika.utils.session_id import resolve_session_tag
from agent.protocols import DIAGNOSIS
from nika.workflows.benchmark.outcomes import (
    COUNTED_OUTCOMES,
    classify_trial_failure,
)
from tests.agent.sandbox_support import (
    sandbox_backend_available,
    sandbox_provider_credential_available,
)
from tests.support.integration_pipeline import (
    DEFAULT_INJECT_PARAMS,
    claude_cli_available,
    codex_cli_available,
    live_anthropic_test_config,
    live_codex_test_model,
    live_custom_base_url,
    load_test_env,
)

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox, pytest.mark.live]

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

CLAUDE_PROVIDER, CLAUDE_MODEL = live_anthropic_test_config()
CODEX_MODEL = live_codex_test_model()


def _diagnosis_mcp_calls(session_path: Path) -> int:
    """Count the NIKA MCP tool calls of the diagnosis phase (not CLI built-ins)."""
    calls = 0
    lines = (session_path / "messages.jsonl").read_text(encoding="utf-8").splitlines()
    for entry in (json.loads(line) for line in lines if line.strip()):
        if entry.get("phase") != DIAGNOSIS:
            continue
        # Claude Code keeps its native stream; NIKA tools are mcp__<server>__*.
        claude = entry.get("claude_event") or {}
        if claude.get("type") == "assistant":
            calls += sum(
                1
                for block in (claude.get("message") or {}).get("content") or []
                if isinstance(block, dict)
                and block.get("type") == "tool_use"
                and str(block.get("name", "")).startswith("mcp__")
            )
        # Codex calls are logged as tool_start events; "bash" is its own shell.
        if entry.get("event") == "tool_start":
            name = str((entry.get("tool") or {}).get("name", ""))
            calls += bool(name) and name != "bash"
    return calls


def _assert_sandbox_artifacts(session_path: Path, *, agent_type: str) -> None:
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

    # The agent reached NIKA's MCP tools from inside the sandbox.
    assert _diagnosis_mcp_calls(session_path), (
        f"no MCP tool calls in the diagnosis under {session_path}: "
        f"{_agent_error(session_path) or 'no agent error'}"
    )


def _assert_scored_session(session_path: Path, *, agent_type: str) -> None:
    """Sandbox artifacts plus a scored outcome.

    Whether the agent solves the task within the step limit depends on the
    model, so a scored failure passes; infrastructure and endpoint failures
    do not.
    """
    run_meta = json.loads((session_path / "run.json").read_text(encoding="utf-8"))
    assert run_meta["outcome"] in COUNTED_OUTCOMES, (
        f"{session_path}: outcome {run_meta['outcome']}: {_agent_error(session_path)}"
    )
    _assert_sandbox_artifacts(session_path, agent_type=agent_type)
    if run_meta["outcome"] == "success":
        assert (session_path / "submission.json").is_file()

    metrics = json.loads((session_path / "eval_metrics.json").read_text())
    for field in (
        "detection_score",
        "localization_accuracy",
        "rca_accuracy",
        "tool_calls",
    ):
        assert field in metrics


def _agent_error(session_path: Path) -> str:
    """Return the message of the session's ``agent_error`` event, or ""."""
    log = session_path / "nika.jsonl"
    lines = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
    for entry in (json.loads(line) for line in lines if line.strip()):
        if entry.get("event") == "agent_error":
            return str(entry.get("message", ""))
    return ""


@contextmanager
def _temp_result_root() -> Iterator[Path]:
    """A benchmark result root owned by one test, removed afterwards."""
    root = Path(tempfile.mkdtemp(prefix="nika-sbx-bench-"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _backend_run_config(backend: str) -> Path:
    """Copy the host run config with *backend* as the sandbox backend."""
    from nika.run_config.loader import resolve_run_config_path

    source = resolve_run_config_path()
    data = (
        yaml.safe_load(source.read_text(encoding="utf-8")) if source.is_file() else {}
    )
    data = data or {}
    data.setdefault("nika", {}).setdefault("sandbox", {})["backend"] = backend
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as handle:
        yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True)
    return Path(handle.name)


def _run_benchmark(
    args: list[str], *, backend: str, agent_type: str, provider: str, model: str
) -> tuple[int, str]:
    """Run ``nika benchmark run`` *args* on the *backend* sandbox.

    Returns its exit code and output.
    """
    run_config = _backend_run_config(backend)
    agent_args = ["-a", agent_type, "-p", provider, "-m", model]
    if provider == "custom":
        agent_args += ["--base-url", live_custom_base_url()]
    try:
        proc = subprocess.run(
            [
                "uv",
                "run",
                "python",
                "-m",
                "tests.support.nika_cli",
                "benchmark",
                "run",
                *args,
                *agent_args,
                "-n",
                "10",
                "-y",
                "-v",
                "--run-config",
                str(run_config),
                "--session-tag",
                resolve_session_tag(context="test"),
            ],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
        )
    finally:
        run_config.unlink(missing_ok=True)
    output = proc.stdout
    if proc.stderr:
        output += proc.stderr
    return proc.returncode, output


def _run_parallel_sandbox_benchmark(
    *,
    backend: str,
    agent_type: str,
    provider: str,
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
                "inject": DEFAULT_INJECT_PARAMS,
            }
        )

    with _temp_result_root() as result_root:
        cases_file = result_root.with_suffix(".yaml")
        cases_file.write_text(
            yaml.dump({"cases": cases}, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        try:
            returncode, output = _run_benchmark(
                [
                    "--config",
                    str(cases_file),
                    "--batch-size",
                    "2",
                    "--result_dir",
                    str(result_root),
                ],
                backend=backend,
                agent_type=agent_type,
                provider=provider,
                model=model,
            )
        finally:
            cases_file.unlink(missing_ok=True)
        # Batch runs score agent failures and still exit 0.
        assert returncode == 0, output

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
            _assert_scored_session(parsed[key], agent_type=agent_type)


class _SandboxBenchmark:
    sandbox_backend = "sbx"
    llm_provider = ""

    def setup_method(self) -> None:
        backend = self.sandbox_backend
        if not sandbox_backend_available(backend):
            pytest.skip(f"{backend} sandbox runtime not available")
        if not sandbox_provider_credential_available(
            self.llm_provider, backend=backend
        ):
            pytest.skip(f"{self.llm_provider} credentials for {backend} not available")


@pytest.mark.skipif(not claude_cli_available(), reason="Claude CLI not available")
class SandboxBenchmarkClaudeTest(_SandboxBenchmark):
    """Claude sandbox path for ``nika benchmark run`` with the selected provider."""

    llm_provider = CLAUDE_PROVIDER

    def test_single_case_produces_sandbox_artifacts(self) -> None:
        with _temp_result_root() as result_root:
            returncode, output = _run_benchmark(
                [
                    "simple_bgp",
                    "--problem",
                    "link_down",
                    "--set",
                    "host_name=pc1",
                    "--set",
                    "intf_name=eth0",
                    "--result_dir",
                    str(result_root),
                ],
                backend=self.sandbox_backend,
                agent_type="cli.claude",
                provider=CLAUDE_PROVIDER,
                model=CLAUDE_MODEL,
            )
            sessions = [path.parent for path in result_root.glob("*/run.json")]
            assert len(sessions) == 1, output
            session_dir = sessions[0]

            if returncode == 0:
                match = _BENCHMARK_DONE_RE.search(output)
                assert match is not None, (
                    f"benchmark_done line missing in output:\n{output}"
                )
                _session_id, scenario, problem, done_dir = match.groups()
                assert (scenario, problem) == ("simple_bgp", "link_down")
                assert Path(done_dir) == session_dir
                _assert_scored_session(session_dir, agent_type="cli.claude")
                return
            # A single case aborts on any agent failure, unlike a batch trial.
            # Accept only a failure the benchmark would score as the agent's.
            error = _agent_error(session_dir)
            assert error, f"no agent_error event under {session_dir}:\n{output}"
            outcome = classify_trial_failure(
                RuntimeError(error), session_dir=session_dir
            )
            assert outcome == "agent_failed", f"{outcome}: {error}\n{output}"
            _assert_sandbox_artifacts(session_dir, agent_type="cli.claude")

    def test_parallel_batch_produces_sandbox_artifacts(self) -> None:
        _run_parallel_sandbox_benchmark(
            backend=self.sandbox_backend,
            agent_type="cli.claude",
            provider=CLAUDE_PROVIDER,
            model=CLAUDE_MODEL,
        )


@pytest.mark.skipif(not codex_cli_available(), reason="Codex CLI not available")
class SandboxBenchmarkCodexTest(_SandboxBenchmark):
    """Codex sandbox path for ``nika benchmark run`` (OpenAI API)."""

    llm_provider = "openai"

    def test_parallel_batch_produces_sandbox_artifacts(self) -> None:
        _run_parallel_sandbox_benchmark(
            backend=self.sandbox_backend,
            agent_type="cli.codex",
            provider="openai",
            model=CODEX_MODEL,
        )


class E2bBenchmarkClaudeTest(SandboxBenchmarkClaudeTest):
    sandbox_backend = "e2b"


class E2bBenchmarkCodexTest(SandboxBenchmarkCodexTest):
    sandbox_backend = "e2b"
