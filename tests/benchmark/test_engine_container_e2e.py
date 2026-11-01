"""A release that pins a benchmark engine runs on that engine's container.

Builds the engine image from the committed ``HEAD`` (uncommitted changes are
not in it), pins it in a copy of release 0.2.0, and runs one task with the
oracle agent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from nika.benchmark_engine.container import (
    RUN_LABEL,
    _git_commit,
    image_id,
    resolve_engine_image,
)
from nika.workflows.benchmark.release import RELEASES_DIR
from nika.workflows.benchmark.run import run_benchmark_from_release
from nika.workflows.benchmark.trials import is_valid_trial
from tests.support.prerequisites import docker_available

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not docker_available(), reason="Docker required"),
]

TASK = "dc_clos__receiver_resource_c__ha2be5b357067"


def test_pinned_release_runs_on_engine_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit = _git_commit("HEAD")[:12]
    engine = {"tag": commit, "api": {"version": 1}}
    built = image_id(f"nika/benchmark-engine:{commit}") is None
    image = resolve_engine_image(engine)
    try:
        release_dir = tmp_path / "releases" / "0.2.0"
        shutil.copytree(RELEASES_DIR / "0.2.0", release_dir)
        manifest = yaml.safe_load((release_dir / "RELEASE.yaml").read_text())
        manifest["engine"] = {**engine, "image": image}
        (release_dir / "RELEASE.yaml").write_text(yaml.safe_dump(manifest))
        monkeypatch.setattr(
            "nika.workflows.benchmark.release.RELEASES_DIR", tmp_path / "releases"
        )

        result_dir = tmp_path / "run"
        run_benchmark_from_release(
            release_ref="0.2.0",
            split="test",
            agent_type="oracle",
            llm_provider=None,
            model=None,
            max_steps=None,
            result_dir=str(result_dir),
            task_ids=[f"{TASK}__t01"],
            yes=True,
        )

        trial = next((result_dir / "trials").iterdir())
        assert is_valid_trial(trial)
        assert json.loads((trial / "run.json").read_text())["outcome"] == "success"
        metrics = json.loads((trial / "eval_metrics.json").read_text())
        assert metrics["rca_f1"] == 1.0
        # The engine's gateway logged the agent's tool calls in the trial.
        tools = (trial / "tool_calls.jsonl").read_text().splitlines()
        assert tools and all(not json.loads(t)["is_error"] for t in tools)

        provenance = json.loads((result_dir / "run.json").read_text())["provenance"]
        assert provenance["engine"]["tag"] == commit
        assert provenance["engine"]["image"] == image_id(image)
        assert provenance["engine"]["commit"].startswith(commit)
        assert provenance["engine"]["dirty"] is False
        assert provenance["engine"]["api"] == 1
        # The engine deployed the environment and served the agent's tools.
        engine_log = (result_dir / "benchmark_engine.log").read_text()
        assert "Started network environment" in engine_log
        assert "Created new" in engine_log  # an MCP transport session

        running = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"label={RUN_LABEL}=0.2.0"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert running.stdout.strip() == ""
    finally:
        if built:
            subprocess.run(["docker", "image", "rm", image], check=False)
