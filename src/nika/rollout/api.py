"""Execute one immutable task attempt through NIKA's benchmark lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from nika.config import REPO_ROOT
from nika.run_config.schema import RunConfig


class RolloutRequest(BaseModel):
    """One episode. Registries and credentials remain host configuration."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    request_id: str = Field(min_length=1)
    task: dict[str, Any]
    run_config: RunConfig
    # Optional external agent project containing nika-agent.yaml.
    agent_project: Path | None = None


class RolloutResult(BaseModel):
    """Terminal outcome with nullable reward for unresolved infrastructure errors."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    request_id: str
    request_sha256: str
    outcome: str
    reward: float | None
    reward_name: str = "rca_f1"
    score_status: str | None = None
    session_id: str | None = None
    returncode: int
    artifacts: dict[str, str]


def run_rollout(request: RolloutRequest, output_dir: Path) -> RolloutResult:
    """Run once in a new directory; never reuse another prediction's cached score.

    The benchmark runner owns preparation, admission, diagnosis, submission,
    grading and cleanup. Cancellation must reach this process group. A caller
    retries by creating a new output directory and request identity.
    """
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    serialized = request.model_dump(mode="json")
    request_body = json.dumps(serialized, sort_keys=True, separators=(",", ":"))
    (output_dir / "request.json").write_text(request_body + "\n")
    config = request.run_config.model_dump(mode="json")
    config["nika"]["result_dir"] = str(output_dir / "native")
    config["benchmark"].update(batch_size=1, retry_passes=0)
    config_path = output_dir / "run-config.yaml"
    config_path.write_text(yaml.safe_dump(config))
    task_path = output_dir / "task.yaml"
    task_path.write_text(yaml.safe_dump({"cases": [request.task]}))
    env = {**os.environ, "NIKA_RUN_CONFIG": str(config_path)}
    if request.agent_project is not None:
        env["NIKA_AGENTS_FILE"] = str(output_dir / "agents.yaml")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "nika.cli.main",
                "agent",
                "add",
                str(request.agent_project.resolve()),
            ],
            env=env,
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    command = [
        sys.executable,
        "-m",
        "nika.cli.main",
        "benchmark",
        "run",
        "--config",
        str(task_path),
        "--run-config",
        str(config_path),
        "--result_dir",
        str(output_dir / "native"),
        "--yes",
    ]
    with (output_dir / "console.log").open("w") as console:
        proc = subprocess.run(
            command, env=env, cwd=REPO_ROOT, stdout=console, stderr=subprocess.STDOUT
        )
    runs = list((output_dir / "native/trials").glob("*/run.json"))
    outcome, reward, session_id, score_status, artifacts = (
        "infra_failed",
        None,
        None,
        None,
        {},
    )
    if len(runs) == 1:
        run_path = runs[0]
        run = json.loads(run_path.read_text())
        metrics_path = run_path.with_name("eval_metrics.json")
        metrics = json.loads(metrics_path.read_text()) if metrics_path.exists() else {}
        outcome = run.get("outcome", "infra_failed")
        reward = (
            metrics.get("rca_f1")
            if outcome == "success"
            else (0.0 if outcome == "agent_failed" else None)
        )
        session_id, score_status = run.get("session_id"), metrics.get("score_status")
        artifacts = {
            p.name: str(p.relative_to(output_dir))
            for p in run_path.parent.iterdir()
            if p.is_file()
        }
    result = RolloutResult(
        request_id=request.request_id,
        request_sha256=hashlib.sha256(request_body.encode()).hexdigest(),
        outcome=outcome,
        reward=reward,
        score_status=score_status,
        session_id=session_id,
        returncode=proc.returncode,
        artifacts=artifacts,
    )
    (output_dir / "result.json").write_text(result.model_dump_json(indent=2) + "\n")
    return result
