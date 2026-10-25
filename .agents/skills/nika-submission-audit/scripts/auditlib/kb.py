"""The audit knowledge base: issue classes, harness policy, and known issues.

The knowledge base is data under the skill's ``kb/`` directory:

- ``taxonomy.yaml``: the issue classes every finding is filed under.
- ``policy.yaml``: what a benchmark trial may use (tools, paths, commands).
- ``issues/*.yaml``: one known issue per file. Each issue says what it is,
  how to recognise it (``detect`` signatures run by the deterministic audit,
  ``guideline`` text for the LLM reviewer), and where it came from.

The audit grows by adding issue files, so every new finding the agentic audit
confirms becomes a signature the next deterministic run checks for free.
"""

from __future__ import annotations

import re
from functools import cached_property
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

KB_DIR = Path(__file__).resolve().parents[2] / "kb"

Severity = Literal["block", "warn", "info"]
Status = Literal["open", "fixed", "by_design", "wontfix"]
SignatureKind = Literal[
    "text",
    "leak_terms",
    "fault_tokens",
    "tool_policy",
    "validity",
    "regrade",
    "mutation",
    "metric",
    "fingerprint_drift",
    "prompt_contrast",
]
StepKindName = Literal[
    "prompt",
    "init",
    "agent_text",
    "reasoning",
    "tool_call",
    "tool_result",
    "harness",
]


class Signature(BaseModel):
    """One machine check for an issue.

    ``text``: ``pattern`` (Python regex, case-insensitive) over the steps of
    kinds ``where``; ``tool`` narrows to calls of matching tool names (and to
    their results). A hit in a tool result whose words also occur in the
    agent's own call input is skipped when ``exclude_authored``.
    ``leak_terms``: benchmark terms (nika, inject, fault, ground truth) in
    agent-visible text.
    ``fault_tokens``: phrases of the trial's own ground-truth fault type
    (``host-incorrect-dns``) in tool output the agent did not type itself.
    ``tool_policy``: tools outside ``policy.yaml``; ``scope`` is ``init``
    (tools the harness offered) or ``call`` (tools the agent used);
    ``pattern`` narrows to matching tool names.
    ``validity``: a run-validity ``flag`` from ``nika.evaluator.validity``.
    ``regrade``: submitted metrics differ from a re-grade; ``explain`` names a
    known scoring revision the difference is consistent with.
    ``mutation``: a state-changing command (``policy.yaml``) in a tool call.
    ``metric``: a submission-level ``metric`` crosses ``threshold`` (see
    :mod:`auditlib.cohort`).
    ``fingerprint_drift``: the harness fingerprint differs across trials.
    ``prompt_contrast``: within an environment, a prompt line shared by at
    least half of the fault tasks and absent from every healthy task.
    """

    model_config = ConfigDict(extra="forbid")

    kind: SignatureKind
    pattern: str | None = None
    where: list[StepKindName] = Field(default_factory=lambda: ["tool_result"])
    tool: str | None = None
    exclude_authored: bool = True
    scope: Literal["init", "call"] | None = None
    flag: str | None = None
    explain: str | None = None
    metric: str | None = None
    threshold: float | None = None
    min_trials: int = 3

    @field_validator("pattern", "tool")
    @classmethod
    def _compiles(cls, value: str | None) -> str | None:
        if value is not None:
            re.compile(value)
        return value

    @model_validator(mode="after")
    def _required(self) -> Signature:
        need = {
            "text": ("pattern",),
            "validity": ("flag",),
            "metric": ("metric", "threshold"),
            "tool_policy": ("scope",),
        }.get(self.kind, ())
        missing = [name for name in need if getattr(self, name) in (None, "")]
        if missing:
            raise ValueError(f"{self.kind} signature needs {', '.join(missing)}")
        return self

    @cached_property
    def regex(self) -> re.Pattern[str] | None:
        return re.compile(self.pattern, re.IGNORECASE) if self.pattern else None

    @cached_property
    def tool_regex(self) -> re.Pattern[str] | None:
        return re.compile(self.tool) if self.tool else None


class Scope(BaseModel):
    """Trials an issue can apply to; empty lists mean any."""

    model_config = ConfigDict(extra="forbid")

    fault_types: list[str] = Field(default_factory=list)
    scenarios: list[str] = Field(default_factory=list)
    agent_types: list[str] = Field(default_factory=list)
    releases: list[str] = Field(default_factory=list)
    healthy: bool | None = None


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    date: str
    refs: list[str] = Field(default_factory=list)
    found_by: Literal["manual", "deterministic", "agentic", "live_audit"] = "manual"


class Issue(BaseModel):
    """A known benchmark, harness, or submission issue.

    ``role: lead`` marks a generic check (an unlisted tool, a mutating command,
    a high healthy false-positive rate) whose hits are leads for the reviewer
    rather than a known root cause. ``exploit: true`` marks a shortcut: trials
    that hit it count as failed in the audited score.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    title: str
    issue_class: str = Field(alias="class")
    severity: Severity
    status: Status
    summary: str
    guideline: str = ""
    scope: Scope = Field(default_factory=Scope)
    detect: list[Signature] = Field(default_factory=list)
    role: Literal["known", "lead"] = "known"
    exploit: bool = False
    fixed_in: str | None = None
    provenance: Provenance
    evidence: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id_shape(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Z][A-Z0-9]*(-[A-Z0-9]+)+", value):
            raise ValueError(f"issue id {value!r} must look like CLASS-NAME-PART")
        return value


class IssueClass(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    question: str
    report_code: str = ""
    references: list[str] = Field(default_factory=list)


class Policy(BaseModel):
    """What a benchmark trial may use."""

    model_config = ConfigDict(extra="forbid")

    allowed_builtin_tools: list[str]
    forbidden_tools: list[str]
    mcp_server_pattern: str
    node_exec_tools: list[str]
    mutating_commands: list[str]
    tool_error_patterns: list[str]
    output_truncated_patterns: list[str]

    @cached_property
    def builtin_regex(self) -> re.Pattern[str]:
        return re.compile("|".join(f"(?:{p})" for p in self.allowed_builtin_tools))

    @cached_property
    def forbidden_regex(self) -> re.Pattern[str]:
        return re.compile("|".join(f"(?:{p})" for p in self.forbidden_tools))

    @cached_property
    def server_regex(self) -> re.Pattern[str]:
        return re.compile(self.mcp_server_pattern)

    @cached_property
    def mutating_regex(self) -> re.Pattern[str]:
        return re.compile("|".join(f"(?:{p})" for p in self.mutating_commands))

    @cached_property
    def tool_error_regex(self) -> re.Pattern[str]:
        return re.compile(
            "|".join(f"(?:{p})" for p in self.tool_error_patterns), re.IGNORECASE
        )

    @cached_property
    def truncated_regex(self) -> re.Pattern[str]:
        return re.compile(
            "|".join(f"(?:{p})" for p in self.output_truncated_patterns), re.IGNORECASE
        )


class KnowledgeBase(BaseModel):
    taxonomy: list[IssueClass]
    policy: Policy
    issues: list[Issue]

    def classes(self) -> dict[str, IssueClass]:
        return {c.id: c for c in self.taxonomy}

    def issue(self, issue_id: str) -> Issue:
        for item in self.issues:
            if item.id == issue_id:
                return item
        raise KeyError(issue_id)


class KnowledgeBaseError(ValueError):
    pass


def _load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise KnowledgeBaseError(f"{path}: {exc}") from exc


def load_kb(kb_dir: Path | None = None) -> KnowledgeBase:
    """Load and cross-check a knowledge base directory.

    The built-in part ships with the skill: ``taxonomy.yaml``, ``policy.yaml``,
    and the generic checks in ``checks/``. The learned part is ``issues/``:
    concrete issues that audits recorded.
    """
    root = Path(kb_dir) if kb_dir is not None else KB_DIR
    files = [root / "taxonomy.yaml", root / "policy.yaml"]
    files += sorted((root / "checks").glob("*.yaml"))
    files += sorted((root / "issues").glob("*.yaml"))
    taxonomy = [IssueClass(**row) for row in _load_yaml(root / "taxonomy.yaml")]
    policy = Policy(**_load_yaml(root / "policy.yaml"))
    issues: list[Issue] = []
    errors: list[str] = []
    for path in files[2:]:
        try:
            issue = Issue(**_load_yaml(path))
        except (TypeError, ValueError) as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        if path.stem != issue.id:
            errors.append(f"{path.name}: file name must be {issue.id}.yaml")
        issues.append(issue)
    known = {c.id for c in taxonomy}
    seen: set[str] = set()
    for issue in issues:
        if issue.issue_class not in known:
            errors.append(f"{issue.id}: unknown class {issue.issue_class!r}")
        if issue.id in seen:
            errors.append(f"{issue.id}: duplicate id")
        seen.add(issue.id)
        if issue.status == "fixed" and not issue.fixed_in:
            errors.append(f"{issue.id}: fixed issues need fixed_in")
    if errors:
        raise KnowledgeBaseError("; ".join(errors))
    return KnowledgeBase(taxonomy=taxonomy, policy=policy, issues=issues)
