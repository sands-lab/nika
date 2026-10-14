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
    assert (session_path / "eval_metrics.json").is_file()

    run_meta = json.loads((session_path / "run.json").read_text(encoding="utf-8"))
    assert run_meta["outcome"] == "success"
    assert (session_path / "submission.json").is_file()

    metrics = json.loads((session_path / "eval_metrics.json").read_text())
    for field in (
        "detection_score",
        "localization_accuracy",
        "rca_accuracy",
        "tool_calls",
    ):
        assert field in metrics


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
) -> str:
    """Run ``nika benchmark run`` *args* on the *backend* sandbox; return its output."""
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
    assert proc.returncode == 0, output
    return output


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

    result_root = Path(tempfile.mkdtemp(prefix="nika-sbx-bench-"))
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as handle:
        yaml.dump({"cases": cases}, handle, sort_keys=False, allow_unicode=True)
        yaml_path = handle.name

    try:
        output = _run_benchmark(
            [
                "--config",
                yaml_path,
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
        output = _run_benchmark(
            [
                "simple_bgp",
                "--problem",
                "link_down",
                "--set",
                "host_name=pc1",
                "--set",
                "intf_name=eth0",
            ],
            backend=self.sandbox_backend,
            agent_type="cli.claude",
            provider=CLAUDE_PROVIDER,
            model=CLAUDE_MODEL,
        )

        match = _BENCHMARK_DONE_RE.search(output)
        assert match is not None, f"benchmark_done line missing in output:\n{output}"
        _session_id, scenario, problem, session_dir = match.groups()
        assert scenario == "simple_bgp"
        assert problem == "link_down"
        _assert_sandbox_session(Path(session_dir), agent_type="cli.claude")

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
