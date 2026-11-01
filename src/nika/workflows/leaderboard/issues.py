"""Stable codes, causes, and fixes for leaderboard validation findings.

Each code has a section in ``docs/benchmarks/leaderboard-submission.md`` whose
anchor is the lower-cased code, so reports can link straight to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nika.workflows.benchmark.official_release import ReleaseReference

GUIDE_URL = (
    "https://github.com/sands-lab/nika/blob/main/docs/benchmarks/"
    "leaderboard-submission.md"
)
# Items listed inline in one-line errors and in text output.
_INLINE_ITEMS = 5
_TEXT_ITEMS = 20


@dataclass(frozen=True)
class IssueSpec:
    title: str
    cause: str
    fix: str


_REPACK = (
    "Re-pack with `nika leaderboard submit --dry-run` from the release run "
    "directory instead of editing package files by hand."
)

ISSUE_SPECS: dict[str, IssueSpec] = {
    "ERROR_PACKAGE_INCOMPLETE": IssueSpec(
        "required package files are missing",
        "The package was not written by `nika leaderboard submit`, or files were "
        "removed after packing.",
        _REPACK,
    ),
    "ERROR_METADATA_INVALID": IssueSpec(
        "metadata.yaml is invalid",
        "A required field is empty, a field is misspelled, or the YAML does not parse.",
        "Compare metadata.yaml with `nika leaderboard template` and fill every "
        "required field.",
    ),
    "ERROR_IDENTITY_INVALID": IssueSpec(
        "results/identity.yaml is invalid",
        "The file was edited by hand or written by a NIKA version with a "
        "different package schema.",
        _REPACK,
    ),
    "ERROR_RESULTS_INVALID": IssueSpec(
        "result files are invalid",
        "A metrics, RCA confusion, or per-trial result file does not match the "
        "package schema, or trial directories do not match their trial ids.",
        _REPACK,
    ),
    "ERROR_RELEASE_UNAVAILABLE": IssueSpec(
        "the official release could not be read",
        "The release has no `v<version>` tag in sands-lab/nika yet, or "
        "GitHub could not be reached.",
        "Check the network connection and that the release version is published. "
        "For an offline check only, pass `--release-source local`; leaderboard "
        "CI still validates against the official release.",
    ),
    "ERROR_BENCHMARK_MISMATCH": IssueSpec(
        "the package identity does not match the release",
        "The run used different release settings (case count, trials per case, "
        "or scoring) than the official release.",
        "Run the official release with `nika benchmark run --release <version>` "
        "and pack that run.",
    ),
    "ERROR_RELEASE_DRIFT": IssueSpec(
        "task definitions differ from the official release",
        "The package was produced with task definitions that do not match the "
        "official release, usually because the NIKA checkout used for the run is "
        "older or newer than the release commit.",
        "Check out the release tag (`git fetch --tags && git checkout "
        "v<version>`), re-run the changed tasks into the same result "
        "directory with `nika benchmark run --release <version> --split <split> "
        "--result_dir <dir> --no-resume --task-id <task> …`, and pack again.",
    ),
    "ERROR_TRIALS_MISSING": IssueSpec(
        "trials are missing",
        "The run did not finish every case × trial slot of the release.",
        "Resume the run with `nika benchmark run --release <version> --split "
        "<split> --result_dir <dir>` until every trial finishes, then pack again.",
    ),
    "ERROR_TRIALS_UNEXPECTED": IssueSpec(
        "the package has trials the release does not define",
        "Trials from another release, split, or ad-hoc run were packed.",
        _REPACK,
    ),
    "ERROR_SCORE_MISMATCH": IssueSpec(
        "reported scores do not match the trial results",
        "metrics.json or rca_confusion.json differs from the aggregate NIKA "
        "recomputes from the per-trial results.",
        _REPACK,
    ),
    "ERROR_GRADING_ERROR": IssueSpec(
        "trials have grading_error status",
        "NIKA could not score these trials.",
        "Repair the grading inputs, re-run the affected trials, and pack again.",
    ),
    "ERROR_INFRA_ERROR": IssueSpec(
        "trials have residual infra_error status",
        "These trials failed for infrastructure reasons and were not retried "
        "successfully.",
        "Resume the run until no infra_error trials remain, then pack again.",
    ),
    "ERROR_RUN_VALIDITY": IssueSpec(
        "harness faults exceed the allowed rate",
        "Too many trials ended with a truncated final turn or an infrastructure "
        "error, which points at the harness rather than the agent.",
        "Fix the harness configuration (for example the output token limit), "
        "re-run the affected trials, and pack again.",
    ),
    "ERROR_UNOFFICIAL_RUN": IssueSpec(
        "the run is not an official release run",
        "Only runs started with `nika benchmark run --release` qualify.",
        "Run the release with `nika benchmark run --release <version>` and pack "
        "that run.",
    ),
    "ERROR_SECRET_FOUND": IssueSpec(
        "package text contains a possible secret or a machine path",
        "A credential or an absolute host path was written into package text.",
        "Remove the value from metadata.yaml or README.md, or from the run "
        "artifacts it came from, and pack again.",
    ),
    "ERROR_TRAJECTORY_INVALID": IssueSpec(
        "trajectory files are missing or invalid",
        "A trial lacks a required trajectory file, or a JSON Lines file does not "
        "parse.",
        _REPACK,
    ),
    "ERROR_EMPTY_TRACE": IssueSpec(
        "trials have no agent trajectory",
        "messages.jsonl records no model or tool interaction, usually because the "
        "agent failed to start or its harness did not record the trace.",
        "Fix the agent or harness setup, re-run the affected trials with "
        "`--no-resume --task-id <trial id>`, and pack again.",
    ),
    "ERROR_TRAJECTORY_FORBIDDEN_FILE": IssueSpec(
        "the trajectories package contains files that must not be published",
        "Packet captures, validation dumps, or judge artifacts were copied into "
        "the package.",
        _REPACK,
    ),
    "ERROR_PACKAGE_PAIR_MISMATCH": IssueSpec(
        "scores and trajectories packages do not match",
        "The two packages were packed from different runs or edited separately.",
        "Re-pack both packages in one `nika leaderboard submit` call.",
    ),
    "WARNING_LOCAL_RELEASE": IssueSpec(
        "validated against local release files",
        "`--release-source local` was set, so this check read the release from "
        "this checkout instead of the official tag.",
        "Validate without `--release-source local` before submitting.",
    ),
    "WARNING_RUN_COMMIT_UNVERIFIED": IssueSpec(
        "the task definitions at the run's NIKA commit could not be checked",
        "The commit recorded in the package is not available locally or on "
        "GitHub, so only trial ids were compared with the official release.",
        "Run from the release tag, or push the commit the run used.",
    ),
    "WARNING_RUN_VALIDITY": IssueSpec(
        "some trials carry run-validity flags",
        "Flags such as no_submission or max_steps_exhausted mark trials whose "
        "score may not reflect the agent's diagnosis.",
        "Review the flagged trials; these flags do not block a submission.",
    ),
    "WARNING_NON_DEFAULT_PRIMARY": IssueSpec(
        "the package uses a non-default primary metric",
        "The release declares a primary metric other than rca_f1.",
        "No action is needed when the release defines this metric.",
    ),
}


def guide_url(code: str) -> str:
    return f"{GUIDE_URL}#{code.lower()}"


@dataclass
class ValidationIssue:
    """One validation finding: a stable code plus the affected items."""

    code: str
    items: list[str] = field(default_factory=list)
    # Overrides the catalog title (for example with counts or a version).
    summary: str | None = None
    # Extra context lines, such as the official commit.
    details: list[str] = field(default_factory=list)
    # Overrides the catalog fix with exact commands.
    fix: str | None = None

    def __post_init__(self) -> None:
        if self.code not in ISSUE_SPECS:
            raise KeyError(f"unknown validation issue code {self.code!r}")

    @property
    def spec(self) -> IssueSpec:
        return ISSUE_SPECS[self.code]

    @property
    def severity(self) -> str:
        return "error" if self.code.startswith("ERROR_") else "warning"

    @property
    def title(self) -> str:
        return self.summary or self.spec.title

    def line(self) -> str:
        """One-line form: code, title, and the first few items."""
        text = f"{self.code}: {self.title}"
        if self.items:
            shown = "; ".join(self.items[:_INLINE_ITEMS])
            more = len(self.items) - _INLINE_ITEMS
            text += f" ({len(self.items)}): {shown}"
            if more > 0:
                text += f"; … (+{more} more)"
        return text

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "title": self.title,
            "details": list(self.details),
            "cause": self.spec.cause,
            "fix": self.fix or self.spec.fix,
            "items": list(self.items),
            "guide_url": guide_url(self.code),
        }

    def render_text(self) -> str:
        lines = [f"{self.code}: {self.title}"]
        lines.extend(f"  {detail}" for detail in self.details)
        if self.items:
            lines.append(f"  Affected ({len(self.items)}):")
            lines.extend(f"    {item}" for item in self.items[:_TEXT_ITEMS])
            if len(self.items) > _TEXT_ITEMS:
                lines.append(f"    … (+{len(self.items) - _TEXT_ITEMS} more)")
        lines.append(f"  Why: {self.spec.cause}")
        lines.append(f"  Fix: {self.fix or self.spec.fix}")
        lines.append(f"  See: {guide_url(self.code)}")
        return "\n".join(lines)

    def render_markdown(self) -> str:
        lines = [f"**{self.code}: {self.title}**", ""]
        lines.extend(f"{detail}  " for detail in self.details)
        if self.details:
            lines.append("")
        lines.append(f"**Why:** {self.spec.cause}")
        lines.append("")
        lines.append(f"**How to fix:** {self.fix or self.spec.fix}")
        lines.append("")
        if self.items:
            lines.append(f"<details><summary>Affected ({len(self.items)})</summary>")
            lines.append("")
            lines.extend(f"- `{item}`" for item in self.items)
            lines.append("")
            lines.append("</details>")
            lines.append("")
        lines.append(f"[Submission guide]({guide_url(self.code)})")
        return "\n".join(lines)


class IssueCollector:
    """Group findings by code, keeping first-seen order."""

    def __init__(self) -> None:
        self._issues: dict[str, ValidationIssue] = {}
        self._extra: list[ValidationIssue] = []

    def add(self, code: str, item: str | None = None) -> None:
        issue = self._issues.setdefault(code, ValidationIssue(code))
        if item is not None:
            issue.items.append(item)

    def extend(self, code: str, items: list[str]) -> None:
        for item in items:
            self.add(code, item)

    def append(self, issue: ValidationIssue) -> None:
        """Add a fully built issue that is not merged with others."""
        self._extra.append(issue)

    @property
    def has_errors(self) -> bool:
        return any(issue.severity == "error" for issue in self.issues)

    @property
    def issues(self) -> list[ValidationIssue]:
        return [*self._extra, *self._issues.values()]


@dataclass
class ValidationReport:
    """Validation findings for one package and the release it was checked against."""

    issues: list[ValidationIssue] = field(default_factory=list)
    reference: ReleaseReference | None = None

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    @property
    def errors(self) -> list[str]:
        return [issue.line() for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[str]:
        return [issue.line() for issue in self.issues if issue.severity == "warning"]

    def raise_if_failed(self) -> None:
        if not self.ok:
            raise LeaderboardValidateError(self.render_text())

    def render_text(self) -> str:
        """Errors first, then warnings, each with its cause and fix."""
        ordered = sorted(self.issues, key=lambda issue: issue.severity != "error")
        blocks = [issue.render_text() for issue in ordered]
        if self.reference is not None:
            blocks.append(f"Validated against {self.reference.describe()}")
        return "\n".join(blocks)

    def to_dict(self, *, package: str | None = None) -> dict[str, Any]:
        return {
            "package": package,
            "ok": self.ok,
            "release": self.reference.to_dict() if self.reference else None,
            "errors": [i.to_dict() for i in self.issues if i.severity == "error"],
            "warnings": [i.to_dict() for i in self.issues if i.severity == "warning"],
        }

    def render_markdown(self, *, package: str) -> str:
        """One package section for a pull request comment."""
        mark = "✅" if self.ok else "❌"
        lines = [f"### {mark} `{package}`", ""]
        if not self.issues:
            lines.append("All checks passed.")
            lines.append("")
        ordered = sorted(self.issues, key=lambda issue: issue.severity != "error")
        for issue in ordered:
            lines.append(issue.render_markdown())
            lines.append("")
        if self.reference is not None:
            lines.append(f"<sub>Validated against {self.reference.describe()}</sub>")
        return "\n".join(lines).rstrip() + "\n"


class LeaderboardValidateError(ValueError):
    """Leaderboard submission failed local validation."""
