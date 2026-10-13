"""Docker E2E: quiet benchmark CLI output + inspect over real mock-agent artifacts.

Requires Docker. Run:

    uv run pytest tests/benchmark/test_run_output_e2e.py -m "e2e and not live" -v
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

from nika.inspect.server import create_inspect_app
from nika.utils.session_id import resolve_session_tag
from tests.benchmark.helpers import inject_params_from_benchmark_yaml
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available

_REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not docker_available(), reason="Docker required"),
]


class TestBenchmarkRunOutputE2E(IntegrationTestCase):
    """One lightweight Kathara case: assert quiet CLI UX and inspect API."""

    def test_quiet_cli_and_inspect(self, tmp_path: Path) -> None:
        inject = inject_params_from_benchmark_yaml("dc_clos", "link_down", "s")
        yaml_path = tmp_path / "cases.yaml"
        yaml_path.write_text(
            yaml.safe_dump(
                {
                    "cases": [
                        {
                            "scenario": "dc_clos",
                            "problem": "link_down",
                            "topo_size": "s",
                            "inject": inject,
                        }
                    ]
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        result_dir = tmp_path / "run-output"
        proc = subprocess.run(
            [
                "uv",
                "run",
                "nika",
                "benchmark",
                "run",
                "--config",
                str(yaml_path),
                "--batch-size",
                "1",
                "--agent",
                "mock",
                "--model",
                "mock-v1",
                "-n",
                "5",
                "-y",
                "--result_dir",
                str(result_dir),
                "--session-tag",
                resolve_session_tag(context="test"),
            ],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        output = proc.stdout + proc.stderr
        assert proc.returncode == 0, output

        assert "Plan:" in output
        assert "1/1 run(s) remaining" in output
        assert "1 case(s) × 1 trial(s)/case" in output
        assert "Pending (1):" in output
        assert "Done: (none)" in output
        assert "NIKA benchmark summary" in output or "rca_f1" in output
        assert "nika inspect --result-dir" in output
        assert "→ start" in output or "✓ done" in output
        assert "dc_clos" in output and "link_down" in output

        # Quiet default: no per-trial operational spam / full paths.
        assert "Running benchmark for Problem:" not in output
        assert "benchmark_done " not in output
        assert " skip (already complete" not in output
        assert "Resuming run:" not in output
        # Third-party MCP/httpx INFO must stay off the default console.
        assert "StreamableHTTP" not in output
        assert "HTTP Request:" not in output
        assert "Negotiated protocol version" not in output
        assert "Processing request of type" not in output

        # Non-TTY progress fallback or live header.
        assert (
            "Progress:" in output
            or "✓ done" in output
            or "avg rca_f1" in output
            or "benchmark" in output.lower()
        )

        trials_root = result_dir / "trials"
        assert trials_root.is_dir()
        trial_dirs = [p for p in trials_root.iterdir() if p.is_dir()]
        assert len(trial_dirs) == 1
        trial_dir = trial_dirs[0]
        for name in (
            "run.json",
            "submission.json",
            "eval_metrics.json",
            "ground_truth.json",
        ):
            assert (trial_dir / name).is_file(), name

        run_meta = json.loads((trial_dir / "run.json").read_text(encoding="utf-8"))
        assert run_meta.get("agent_type") == "mock"
        assert run_meta.get("status") == "finished"
        session_id = str(run_meta["session_id"])

        app = create_inspect_app(results_root=result_dir)
        client = TestClient(app)
        listed = client.get("/api/sessions")
        assert listed.status_code == 200, listed.text
        payload = listed.json()
        sessions = payload.get("sessions") or []
        assert sessions, payload
        assert any(s.get("session_id") == session_id for s in sessions), sessions

        detail = client.get(f"/api/sessions/{session_id}")
        assert detail.status_code == 200, detail.text
        scores = client.get(f"/api/sessions/{session_id}/scores")
        assert scores.status_code == 200, scores.text
        timeline = client.get(f"/api/sessions/{session_id}/timeline")
        assert timeline.status_code == 200, timeline.text

        # Verbose resume when already complete: plan shows zero pending.
        verbose_proc = subprocess.run(
            [
                "uv",
                "run",
                "nika",
                "benchmark",
                "run",
                "--config",
                str(yaml_path),
                "--agent",
                "mock",
                "--model",
                "mock-v1",
                "-n",
                "5",
                "-y",
                "-v",
                "--result_dir",
                str(result_dir),
                "--session-tag",
                resolve_session_tag(context="test"),
            ],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        verbose_out = verbose_proc.stdout + verbose_proc.stderr
        assert verbose_proc.returncode == 0, verbose_out
        assert "All 1 trial(s) already complete" in verbose_out or "0/" in verbose_out
