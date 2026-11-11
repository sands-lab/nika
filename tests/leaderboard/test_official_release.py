"""Leaderboard checks against the official ``v<version>`` reference."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import replace

import pytest
import yaml
from typer.testing import CliRunner

from nika.cli.main import app
from nika.config import REPO_ROOT
from nika.workflows.benchmark import official_release
from nika.workflows.benchmark.official_release import (
    OfficialReleaseUnavailable,
    resolve_release_commit,
)
from nika.workflows.benchmark.trials import task_id_for_row
from nika.workflows.leaderboard.issues import ISSUE_SPECS
from nika.workflows.leaderboard.pack import LeaderboardPackError
from nika.workflows.leaderboard.validate import validate_leaderboard_submission
from tests.leaderboard.test_pack_validate import _pack, mini_release_env  # noqa: F401
from tests.support.official_release import FakeOfficialRepo

pytestmark = pytest.mark.contract


@pytest.mark.parametrize("setting", ["engine", "evaluation"])
def test_pinned_engine_and_policy_changes_are_release_drift(
    mini_release_env,  # noqa: F811
    setting,
) -> None:
    release, _result_dir = mini_release_env
    changed = replace(
        release, manifest={**release.manifest, setting: {"changed": True}}
    )
    drift = official_release.release_drift(release, changed)
    assert drift.changed_settings == [setting]
    assert drift.drifted


def _error_codes(report) -> list[str]:
    return [issue.code for issue in report.issues if issue.severity == "error"]


def _publish_edited(
    fake: FakeOfficialRepo, release, edit, *, tag: bool = True
) -> tuple[str, str]:
    """Publish a copy of ``release`` with ``edit`` applied to its only case.

    Returns ``(commit, official task id)``.
    """
    files = {path.name: path.read_bytes() for path in release.root.glob("*.yaml")}
    cases_name = release.cases_path.name
    payload = yaml.safe_load(files[cases_name])
    edit(payload["cases"][0])
    files[cases_name] = yaml.safe_dump(payload, sort_keys=False).encode()
    commit = fake.publish(release.version, files)
    if tag:
        fake.tag(release.version, commit)
    edited = official_release.load_release_at(
        release.version, commit, split=release.split
    )
    return commit, task_id_for_row(edited.cases[0])


def _move_inject(case: dict) -> None:
    case["inject"]["host_name"] = "client_1"


def _add_root_cause(case: dict) -> None:
    case["root_causes"] = [
        {"resource": {"kind": "node", "node": "client_0"}, "fault_type": "link_down"}
    ]


def test_matching_release_validates_against_the_tag(
    mini_release_env,  # noqa: F811
    official_releases: FakeOfficialRepo,
) -> None:
    release, result_dir = mini_release_env
    package = _pack(result_dir)

    report = validate_leaderboard_submission(package)

    assert report.ok, report.errors
    assert report.reference is not None
    assert report.reference.source == "official"
    assert report.reference.tag == f"v{release.version}"
    assert report.reference.commit == official_releases.resolve(release.version)


def test_drifted_release_reports_one_drift_error(
    mini_release_env,  # noqa: F811
    official_releases: FakeOfficialRepo,
) -> None:
    release, result_dir = mini_release_env
    package = _pack(result_dir)
    commit, official_task = _publish_edited(official_releases, release, _move_inject)

    report = validate_leaderboard_submission(package)

    assert _error_codes(report) == ["ERROR_RELEASE_DRIFT"]
    drift = report.issues[0]
    assert drift.items == [official_task]
    assert any(commit in detail for detail in drift.details)
    assert f"git worktree add <release-checkout> {commit}" in (drift.fix or "")

    with pytest.raises(LeaderboardPackError, match="ERROR_RELEASE_DRIFT"):
        _pack(result_dir)


def test_ground_truth_change_at_run_commit_is_drift(
    mini_release_env,  # noqa: F811
    official_releases: FakeOfficialRepo,
) -> None:
    """A changed root cause keeps the trial ids; the run's commit reveals it."""
    release, result_dir = mini_release_env
    run_commit = official_releases.resolve(release.version)
    run_path = result_dir / "run.json"
    run_cfg = json.loads(run_path.read_text(encoding="utf-8"))
    run_cfg["nika_git_commit"] = run_commit
    run_path.write_text(json.dumps(run_cfg), encoding="utf-8")
    package = _pack(result_dir)
    _commit, official_task = _publish_edited(
        official_releases, release, _add_root_cause
    )
    assert official_task == task_id_for_row(release.cases[0])

    report = validate_leaderboard_submission(package)

    assert _error_codes(report) == ["ERROR_RELEASE_DRIFT"]
    assert report.issues[0].items == [official_task]


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("engine", {"tag": "different-engine", "api": {"version": 1}}),
        ("evaluation", {"max_trial_sec": 30}),
        ("images", {"required": ["different/image:tag"]}),
    ],
)
def test_run_commit_with_different_release_settings_is_rejected(
    mini_release_env,  # noqa: F811
    official_releases: FakeOfficialRepo,
    setting,
    value,
) -> None:
    release, result_dir = mini_release_env
    package = _pack(result_dir)
    files = {p.name: p.read_bytes() for p in release.root.glob("*.yaml")}
    manifest = yaml.safe_load(files["RELEASE.yaml"])
    manifest[setting] = value
    files["RELEASE.yaml"] = yaml.safe_dump(manifest).encode()
    commit = official_releases.publish(release.version, files)
    identity_path = package / "results/identity.yaml"
    identity = yaml.safe_load(identity_path.read_text())
    identity["run"]["nika_git_commit"] = commit
    identity_path.write_text(yaml.safe_dump(identity))

    report = validate_leaderboard_submission(package)

    assert _error_codes(report) == ["ERROR_BENCHMARK_MISMATCH"]
    assert setting in " ".join(report.issues[0].items)
    run_path = result_dir / "run.json"
    run = json.loads(run_path.read_text())
    run["nika_git_commit"] = commit
    run_path.write_text(json.dumps(run))
    with pytest.raises(LeaderboardPackError, match="ERROR_BENCHMARK_MISMATCH"):
        _pack(result_dir)


def test_unreachable_release_fails_unless_local(
    mini_release_env,  # noqa: F811
    official_releases: FakeOfficialRepo,
) -> None:
    _release, result_dir = mini_release_env
    package = _pack(result_dir)
    official_releases.unreachable = True

    report = validate_leaderboard_submission(package)
    assert _error_codes(report) == ["ERROR_RELEASE_UNAVAILABLE"]

    local = validate_leaderboard_submission(package, release_source="local")
    assert local.ok, local.errors
    assert "WARNING_LOCAL_RELEASE" in [issue.code for issue in local.issues]
    assert local.reference is not None and local.reference.source == "local"


def test_validate_cli_reports_codes_as_json_and_markdown(
    mini_release_env,  # noqa: F811
    official_releases: FakeOfficialRepo,
) -> None:
    release, result_dir = mini_release_env
    package = _pack(result_dir)
    _publish_edited(official_releases, release, _move_inject)
    runner = CliRunner()

    result = runner.invoke(
        app, ["leaderboard", "validate", str(package), "--format", "json"]
    )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert [error["code"] for error in payload["errors"]] == ["ERROR_RELEASE_DRIFT"]
    assert payload["errors"][0]["guide_url"].endswith("#error_release_drift")
    assert payload["release"]["tag"] == f"v{release.version}"

    result = runner.invoke(
        app, ["leaderboard", "validate", str(package), "--format", "markdown"]
    )
    assert result.exit_code == 1
    assert "**ERROR_RELEASE_DRIFT: task definitions differ" in result.stdout
    assert "<details><summary>Affected (1)</summary>" in result.stdout


def _ls_remote(stdout: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kwargs):
        assert cmd[:2] == ["git", "ls-remote"]
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(official_release.subprocess, "run", fake_run)


def test_resolve_release_commit_reads_the_tagged_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tag_object, commit = "a" * 40, "b" * 40
    _ls_remote(
        f"{tag_object}\trefs/tags/v9.9.9\n{commit}\trefs/tags/v9.9.9^{{}}\n",
        monkeypatch,
    )
    # Annotated tag: the peeled ``^{}`` entry names the commit.
    assert resolve_release_commit("9.9.9") == commit

    _ls_remote("", monkeypatch)
    with pytest.raises(OfficialReleaseUnavailable, match="no tag v9.9.9"):
        resolve_release_commit("9.9.9")


@pytest.mark.parametrize("version", ["0.1.0", "0.2.0"])
def test_legacy_release_reference_does_not_depend_on_a_missing_tag(
    monkeypatch, version
):
    def unexpected_network(*args, **kwargs):
        pytest.fail("A legacy release uses its declared published commit")

    monkeypatch.setattr(official_release.subprocess, "run", unexpected_network)
    assert resolve_release_commit(version) == "ac4422d8a4a03be2b905e28edf72993d467e1b81"


def test_every_issue_code_has_a_guide_section() -> None:
    """Validation reports link each code to ``#<code>`` in the submission guide."""
    guide = (REPO_ROOT / "docs/benchmarks/leaderboard-submission.md").read_text(
        encoding="utf-8"
    )
    headings = set(re.findall(r"^### `([A-Z_]+)`$", guide, flags=re.MULTILINE))
    assert headings == set(ISSUE_SPECS)
