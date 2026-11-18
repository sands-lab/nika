"""GitHub Actions entry point for paired validation and transcript review."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import yaml

from nika.audit.fetch import TrajectoryPendingError, fetch_trajectories
from nika.audit.review import load_issue_file
from nika.audit.runner import render_markdown, write_report
from nika.audit.trace import load_trial, trial_dirs
from nika.workflows.leaderboard.review import review_trajectories
from nika.workflows.leaderboard.schema import PackageIdentity
from nika.workflows.leaderboard.secrets import reject_package_symlinks, scan_package_dir
from nika.workflows.leaderboard.validate import validate_leaderboard_submission

REVIEW_COMMAND = (
    "codex exec --ignore-user-config --ephemeral --skip-git-repo-check "
    "--sandbox read-only --disable shell_tool --disable unified_exec "
    "--model gpt-6-luna -"
)


def publish_proposals(
    package: Path, source: dict, review: Path, trajectories: Path
) -> None:
    """Open deduplicated candidate issues and a draft knowledge PR."""
    summary = json.loads((review / "summary.json").read_text())
    if not summary["complete"] or not summary["written"]:
        return
    # Publication uses the runner operator's configured gh account, not the caller's token.
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"GH_TOKEN", "GITHUB_TOKEN", "HF_TOKEN"}
    }
    target = "sands-lab/nika"
    branch = f"bot/audit/{source['path'].split('/')[1]}/{package.name}-{source['revision'][:12]}"

    def gh(*args: str) -> str:
        return subprocess.check_output(["gh", *args], env=env, text=True).strip()

    prs = json.loads(
        gh(
            "pr",
            "list",
            "--repo",
            target,
            "--state",
            "all",
            "--head",
            branch,
            "--json",
            "state,url",
        )
    )
    if any(pr["state"] != "OPEN" for pr in prs):
        return
    with tempfile.TemporaryDirectory(prefix="nika-audit-proposal-") as directory:
        checkout = Path(directory) / "nika"

        def git(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [
                    "git",
                    "-c",
                    "credential.helper=",
                    "-c",
                    "credential.helper=!gh auth git-credential",
                    *args,
                ],
                cwd=checkout if checkout.exists() else directory,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        git(
            "clone",
            "--filter=blob:none",
            "--single-branch",
            "--branch",
            "dev",
            f"https://github.com/{target}.git",
            str(checkout),
        )
        if git("ls-remote", "--heads", "origin", branch).stdout.strip():
            git("fetch", "origin", branch)
            git("switch", "-c", branch, "FETCH_HEAD")
        else:
            git("switch", "-c", branch)
        git("config", "user.name", "Zhihao Wang")
        git("config", "user.email", "zhihao.wang@kaust.edu.sa")
        candidates = [
            load_issue_file(review / "kb/issues" / f"{name}.yaml")
            for name in sorted(set(summary["written"]))
        ]
        for issue in candidates:
            relative = Path("src/nika/audit/kb_data/issues") / f"{issue.id}.yaml"
            destination = checkout / relative
            if not destination.exists():
                shutil.copy2(review / "kb/issues" / destination.name, destination)
                git("add", str(relative))
        staged = git("diff", "--cached", "--name-only").stdout.strip()
        if staged:
            git(
                "commit",
                "-m",
                f"feat(audit): propose knowledge from {package.name}",
                "-m",
                "Generated candidate rules require maintainer evidence review.",
                "-m",
                "Co-authored-by: Codex <codex@openai.com>",
            )
            git("push", "origin", f"HEAD:refs/heads/{branch}")
        elif not prs:
            # A prior push may have succeeded before PR creation failed.
            if not git("diff", "origin/dev...HEAD", "--name-only").stdout.strip():
                return
        proposals = review / "proposals"
        proposals.mkdir(exist_ok=True)
        run = f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        dataset = f"https://huggingface.co/datasets/{source['repo']}/tree/{source['revision']}/{source['path']}"
        links = []
        trials = {path.name: path for path in trial_dirs(trajectories)}
        for issue in candidates:
            marker = f"<!-- nika-audit:{issue.id} -->"
            found = json.loads(
                gh(
                    "issue",
                    "list",
                    "--repo",
                    target,
                    "--state",
                    "all",
                    "--search",
                    f"{issue.id} in:body",
                    "--json",
                    "body,url",
                    "--limit",
                    "100",
                )
            )
            existing = next(
                (row["url"] for row in found if marker in row["body"]), None
            )
            body = proposals / f"{issue.id}.md"
            excerpts = []
            for ref in issue.provenance.refs[:5]:
                trial, _, step = ref.rpartition(":")
                if trial in trials and step.isdigit():
                    observed = load_trial(trials[trial]).steps
                    if int(step) < len(observed):
                        excerpts.append(
                            f"`{ref}` (JSON-escaped excerpt):\n```json\n{json.dumps(observed[int(step)].text[:600])}\n```"
                        )
            body.write_text(
                f"{marker}\nCandidate `{issue.id}` requires maintainer review.\n\n"
                f"{issue.summary}\n\n[Immutable trajectories]({dataset}) · [Audit evidence]({run})\n\n"
                + "References:\n"
                + "\n".join(f"- `{ref}`" for ref in issue.provenance.refs)
                + "\n\n"
                + "\n\n".join(excerpts)
                + "\n\nThe proposed detector and its scope are included in the linked knowledge PR.\n"
            )
            links.append(
                existing
                or gh(
                    "issue",
                    "create",
                    "--repo",
                    target,
                    "--title",
                    f"[audit proposal] {issue.title}",
                    "--body-file",
                    str(body),
                )
            )
        body = proposals / "pull-request.md"
        body.write_text(
            f"Propose trajectory-audit knowledge from `{package.name}`.\n\n[Source]({dataset}) · [Audit evidence]({run})\n\n"
            + "\n".join(f"Refs {link}" for link in links)
            + "\n\nVerify the cited steps and detector scope before accepting these rules.\n"
        )
        if prs:
            update = proposals / "pr-update.json"
            update.write_text(json.dumps({"body": body.read_text()}))
            gh(
                "api",
                "--method",
                "PATCH",
                f"repos/{target}/pulls/{prs[0]['url'].rsplit('/', 1)[1]}",
                "--input",
                str(update),
            )
            update.unlink()
        else:
            gh(
                "pr",
                "create",
                "--repo",
                target,
                "--base",
                "dev",
                "--head",
                branch,
                "--draft",
                "--title",
                f"feat(audit): review knowledge from {package.name}",
                "--body-file",
                str(body),
            )


def audit_package(
    package: Path,
    out: Path,
    repo: Path,
    *,
    trajectories_repo: str,
    revision: str | None = None,
    review: bool = False,
) -> bool:
    """Fetch one immutable package and run a CI stage against its trajectories."""
    reject_package_symlinks(package)
    identity = PackageIdentity.model_validate(
        yaml.safe_load((package / "results/identity.yaml").read_text())
    )
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nika-audit-input-") as directory:
        root = Path(directory)
        deadline = time.monotonic() + 600
        while True:
            try:
                trajectories = fetch_trajectories(
                    package.name,
                    identity.benchmark.version,
                    root,
                    repo=trajectories_repo,
                    revision=revision,
                )
                break
            except TrajectoryPendingError:
                if review or time.monotonic() >= deadline:
                    raise
                print("Waiting for the paired trajectories upload.", flush=True)
                time.sleep(30)
        reject_package_symlinks(trajectories)
        source = json.loads((root / "fetch-source.json").read_text())
        (out / "source.json").write_text(json.dumps(source, indent=2) + "\n")
        if os.environ.get("GITHUB_OUTPUT"):
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
                output.write(f"revision={source['revision']}\n")
        if review:
            summary = review_trajectories(
                trajectories,
                out / "review",
                command=REVIEW_COMMAND,
                repo=repo,
                workers=min(8, os.cpu_count() or 1),
            )
            if os.environ.get("GITHUB_ACTIONS") == "true":
                publish_proposals(package, source, out / "review", trajectories)
            return summary["complete"]
        report = validate_leaderboard_submission(
            package,
            trajectories_dir=trajectories,
            audit_repo=repo,
        )
        if os.environ.get("GITHUB_OUTPUT"):
            reviewable = report.audit is not None and not scan_package_dir(package)
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
                output.write(f"reviewable={str(reviewable).lower()}\n")
        (out / "validation.json").write_text(
            json.dumps(
                {
                    "ok": report.ok,
                    "errors": report.errors,
                    "warnings": report.warnings,
                },
                indent=2,
            )
            + "\n"
        )
        text = "\n".join(report.errors + report.warnings)
        if report.audit is not None:
            report.audit["trajectory_source"] = source
            write_report(report.audit, out / "audit.json")
            text += "\n" + render_markdown(report.audit)
        (out / "summary.md").write_text(text + "\n")
        print(text)
        return report.ok


def main() -> int:
    root = Path(os.environ["SCORES_ROOT"]).resolve()
    package = (root / os.environ["PACKAGE"]).resolve()
    if not package.is_relative_to(root) or not package.is_dir():
        raise ValueError(
            "Scores package must be a directory inside the scores checkout."
        )
    review = os.environ["AUDIT_STAGE"] == "review"
    revision = os.environ.get("TRAJECTORY_REVISION") or None
    if review and not revision:
        raise ValueError("Model review requires the validated trajectory revision.")
    ok = audit_package(
        package,
        Path(os.environ["AUDIT_OUT"]),
        Path(os.environ["NIKA_ROOT"]),
        trajectories_repo=os.environ["TRAJECTORIES_REPO"],
        revision=revision,
        review=review,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
