"""Review recorded trajectories and keep proposed rules separate from the KB."""

from __future__ import annotations

import json
import os
import shlex
import shutil
from dataclasses import replace
from pathlib import Path

from nika.audit.cohort import history_from_scores
from nika.audit.kb import KB_DIR, load_kb
from nika.audit.pipeline import review_submission
from nika.audit.runner import audit_trajectories, read_package, write_report
from nika.audit.scan import ScanConfig
from nika.audit.trace import trial_dirs


def review_trajectories(
    trajectories: Path,
    workdir: Path,
    *,
    command: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    kb: Path | None = None,
    repo: Path | None = None,
    history: Path | None = None,
    samples: int = 3,
    workers: int = 2,
) -> dict:
    """Run model review in a new directory and replay proposed rules."""
    argv = tuple(shlex.split(command)) if command else None
    if command is not None and not argv:
        raise ValueError("Reviewer command must not be empty.")
    if argv is None and not (base_url and model):
        raise ValueError("Provide --command or both --base-url and --model.")
    if samples < 1 or workers < 1:
        raise ValueError("Samples and workers must be positive.")
    if not trial_dirs(trajectories):
        raise ValueError("No trial directories found.")
    seed = (kb or KB_DIR).resolve()
    workdir = workdir.resolve()
    for source in (trajectories.resolve(), seed):
        if workdir.is_relative_to(source) or source.is_relative_to(workdir):
            raise ValueError(
                "Review output must be separate from trajectories and the KB."
            )
    load_kb(seed)
    past = []
    if history:
        release = read_package(trajectories).package.get("release") or ""
        past = history_from_scores(
            history, release=str(release), exclude=trajectories.name
        )
    workdir.mkdir(parents=True, exist_ok=False)
    candidate = workdir / "kb"
    shutil.copytree(seed, candidate)
    scan = ScanConfig(
        base_url=base_url or "",
        model=model or command,
        command=argv,
        api_key=os.environ.get("NIKA_AUDIT_LLM_KEY", "none"),
        samples=samples,
        workers=workers,
        thinking=False,
    )
    summary = review_submission(
        trajectories,
        candidate,
        workdir,
        scan=scan,
        adjudicate=replace(scan, thinking=True, max_tokens=16384),
        history=past,
        repo=repo,
    )
    load_kb(candidate)
    summary["complete"] = not any(summary["errors"]) and not any(
        stage.get("errors")
        or stage.get("scan_errors")
        or stage.get("judge_errors")
        or stage.get("unsupported")
        for stage in summary.values()
        if isinstance(stage, dict)
    )
    if summary["written"]:
        write_report(
            audit_trajectories(
                trajectories, kb=load_kb(candidate), history=past, repo=repo
            ),
            workdir / "audit-candidate.json",
        )
    (workdir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    return summary
