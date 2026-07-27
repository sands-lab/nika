"""Opt-in live GitHub submit E2E (draft PR then close).

Requires:
  export NIKA_LEADERBOARD_E2E=1
  authenticated ``gh`` with push access to sands-lab/nika-leaderboard
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from nika.workflows.leaderboard import github_cli as gh
from nika.workflows.leaderboard.remote import DEFAULT_LEADERBOARD_REPO
from nika.workflows.leaderboard.submit import submit_leaderboard_package

_LIVE = os.environ.get("NIKA_LEADERBOARD_E2E", "").strip() in {"1", "true", "yes"}
_gh_ok = shutil.which("gh") is not None

pytestmark = [
    pytest.mark.skipif(not _LIVE, reason="set NIKA_LEADERBOARD_E2E=1 to run"),
    pytest.mark.skipif(not _gh_ok, reason="gh CLI not on PATH"),
]


def _minimal_live_package(tmp_path: Path) -> Path:
    """Package used only to exercise git/PR plumbing (skip_validate=True)."""
    import json

    import yaml

    stamp = time.strftime("%Y%m%d")
    # Unique slug so repeated runs do not collide with an existing remote path.
    uniq = f"nika_e2e_{int(time.time())}"
    root = tmp_path / f"{stamp}_{uniq}"
    root.mkdir(parents=True)
    (root / "metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "info": {
                    "name": f"NIKA E2E {uniq}",
                    "authors": "NIKA CI",
                },
                "agent": {
                    "model": "mock",
                    "framework": "mock",
                    "tools": [],
                    "skills": [],
                    "optimization_methods": [],
                    "tags": ["e2e"],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        "# NIKA live submit E2E\n\nTemporary draft PR; will be closed by CI.\n",
        encoding="utf-8",
    )
    (root / "files.json").write_text(
        json.dumps({"source_run_sha256": "e2e", "package": {}}) + "\n",
        encoding="utf-8",
    )
    results = root / "results"
    results.mkdir()
    (results / "identity.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1",
                "benchmark": {
                    "id": "nika-bench",
                    "version": "0.1.0",
                    "digest": "0" * 64,
                    "split": "test",
                    "cases_sha256": "0" * 64,
                    "case_count": 1,
                    "n_trials": 1,
                    "scoring_id": "rule-based-v1",
                    "leaderboard_primary": "rca_f1",
                },
                "run": {
                    "run_id": "e2e",
                    "official": True,
                    "agent_type": "mock",
                    "model": "mock",
                    "n_trials": 1,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (results / "metrics.json").write_text("{}\n", encoding="utf-8")
    trial = results / "trials" / "dummy__t01"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text("{}\n", encoding="utf-8")
    return root


def test_live_submit_opens_and_closes_draft_pr(tmp_path: Path) -> None:
    gh.ensure_gh_auth()
    repo = DEFAULT_LEADERBOARD_REPO
    if not gh.can_push(repo):
        pytest.skip(f"no push access to {repo}")

    package = _minimal_live_package(tmp_path)
    result = submit_leaderboard_package(
        package,
        repo=repo,
        draft=True,
        skip_validate=True,
        title=f"[e2e] temporary submit {package.name}",
        body=(
            "Automated NIKA leaderboard submit E2E draft PR.\n\n"
            "Safe to close; opened by `tests/leaderboard/test_e2e_submit_github.py`."
        ),
    )
    assert result.pr_url.startswith("https://github.com/")
    assert result.remote_path == f"submissions/0.1.0/{package.name}"
    assert package.name in result.branch

    number = gh.pr_number_from_url(result.pr_url)
    try:
        # Confirm the path landed on the PR head.
        files = gh.run_command(
            [
                gh.require_gh(),
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "files",
                "--jq",
                ".files[].path",
            ]
        ).stdout
        assert result.remote_path in files or any(
            result.remote_path in line for line in files.splitlines()
        )
    finally:
        gh.close_pull_request(
            repo,
            number,
            comment="Closing automated NIKA leaderboard E2E draft PR.",
        )
