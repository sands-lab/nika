"""Render the release environment-audit reference.

The page lists every case in release 0.2.0. A case the full audit did not
execute stays ``not_run``. That status is not a pass.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from nika.audit.coverage import cover_release, gap_count, release_cases
from nika.audit.environment import CaseAudit, identity_from_row
from tests.audit.live import declared_probe

DOC_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "development"
    / "environment-audit.md"
)


def _design(identity) -> str:
    parts: list[str] = []
    if identity.igp:
        parts.append(f"igp={identity.igp}")
    if identity.bgp_mode:
        parts.append(f"bgp_mode={identity.bgp_mode}")
    if identity.rpki != "":
        parts.append(f"rpki={identity.rpki}")
    if identity.device_profile:
        parts.append(f"device_profile={identity.device_profile}")
    return ", ".join(parts) if parts else "none"


def _inject(identity) -> str:
    if not identity.inject:
        return "none"
    return ", ".join(f"{key}={identity.inject[key]}" for key in sorted(identity.inject))


def _cell(value: str) -> str:
    return value.replace("|", "\\|")


def render_environment_audit_doc(
    *,
    version: str = "0.2.0",
    audits: list[CaseAudit] | None = None,
) -> str:
    """Return the reference page for release ``version``."""
    rows = release_cases(version)
    for row in rows:
        row["symptom_probe"] = (
            ""
            if row.get("problem") == "healthy"
            else declared_probe(str(row["problem"]))
        )
    live = list(audits or [])
    covered = cover_release(rows, live)
    counts = gap_count(covered)
    probes = Counter(str(item["symptom_probe"] or "healthy") for item in covered)
    lines = [
        "# Environment audit",
        "",
        f"This reference lists every case in benchmark release {version}.",
        "A case is the scenario, scale, backend, design options, fault, and inject parameters.",
        "",
        "A running benchmark trial and a full audit use different checks.",
        "",
        "The trial calls `startup_verify_lab` when the lab starts and `verify_fault` after inject.",
        "While the agent runs, and again before NIKA removes the lab, `PresenceWatch` reads the fault artifact on the problem instance that injected the fault.",
        "The trial writes those reads to `fault-presence.json`.",
        "A `present` result means the artifact was still on the lab.",
        "The full audit records the network effect.",
        "When the artifact is absent or the read fails, the trial outcome is `environment_invalid`.",
        "Leaderboard averages omit that outcome.",
        "`nika benchmark run --resume` deletes the slot and runs it again.",
        "",
        "Faults whose effect is a live worker, flap, queue, or quota are listed in `DYNAMIC_ARTIFACT_FAULTS`.",
        "The recheck reads that worker, queue, or quota on the injected instance.",
        "",
        "Run the full audit through `audit_case` in `tests/audit/live.py`.",
        "Benchmark runs stay on `startup_verify_lab`, `verify_fault`, and `PresenceWatch`.",
        "For one selected case, `audit_case` deploys a lab and runs `verify_lab` plus a healthy probe of the fault path before inject.",
        "After inject it runs `verify_fault`, the symptom probe, and a control-path observation.",
        "`window_for` chooses how long that fault stays in place before the next read.",
        "After that wait, `audit_case` reads the artifact and the symptom probe again.",
        "Those two reads run once more before `audit_case` undeploys the session it created.",
        "A case with fault `healthy` runs `verify_lab` before the window and again before cleanup.",
        "",
        "## Admission",
        "",
        "A case is admitted only when `admission` is `pass`.",
        "",
        "| Status | Meaning |",
        "| --- | --- |",
        "| `pass` | Every recorded stage observed the expected condition. |",
        "| `fail` | A stage ran and the observation failed. |",
        "| `skipped` | The audit ran and skipped the stage. |",
        "| `unsupported` | This fault has no behavioral check for the stage. `reason` names the scope. |",
        "| `no_evidence` | The stage produced no observation. An `artifact_only` symptom result is `no_evidence`. |",
        "| `not_run` | The full audit did not execute this case. |",
        "",
        "`fail`, `skipped`, `unsupported`, `no_evidence`, and `not_run` do not admit a case.",
        "",
        "## Regenerate this page",
        "",
        "From the repository root:",
        "",
        "```shell",
        'uv run python -c "from tests.audit.report_doc import write_environment_audit_doc; write_environment_audit_doc()"',
        "```",
        "",
        "## Coverage",
        "",
        f"Release {version} has {counts['cases']} cases ({_split_counts(covered)}).",
        f"Admitted cases: {counts['admitted']}.",
        f"{probes.get('artifact_only', 0)} cases declare an `artifact_only` symptom probe.",
        "A full audit records `no_evidence` for that symptom stage.",
        "",
        "| Status | Cases |",
        "| --- | --- |",
    ]
    for status in ("pass", "fail", "skipped", "unsupported", "no_evidence", "not_run"):
        lines.append(f"| `{status}` | {counts[status]} |")
    lines.extend(
        [
            "",
            "Symptom probes declared for these cases:",
            "",
            "| Probe | Cases |",
            "| --- | --- |",
        ]
    )
    for probe, count in sorted(probes.items()):
        label = probe or "healthy"
        lines.append(f"| `{label}` | {count} |")
    lines.extend(["", "## Cases", ""])
    by_scenario: dict[str, list[dict]] = {}
    for item in covered:
        scenario = item["identity"].scenario
        by_scenario.setdefault(scenario, []).append(item)
    for scenario, items in by_scenario.items():
        lines.extend(
            [
                f"### `{scenario}`",
                "",
                "| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for item in items:
            identity = item["identity"]
            backend = identity.backend or "scenario default"
            scale = identity.topo_size or "none"
            probe = item["symptom_probe"] or "healthy"
            lines.append(
                "| "
                + " | ".join(
                    _cell(part)
                    for part in (
                        str(item["split"]),
                        identity.fault,
                        scale,
                        backend,
                        _design(identity),
                        _inject(identity),
                        probe,
                        str(item["admission"]),
                    )
                )
                + " |"
            )
        lines.append("")
    lines.append("Rows with admission `not_run` are coverage gaps for the full audit.")
    lines.append("")
    return "\n".join(lines)


def _split_counts(covered: list[dict]) -> str:
    counts = Counter(str(item["split"]) for item in covered)
    return ", ".join(
        f"{name} {counts[name]}" for name in ("dev", "test") if counts[name]
    )


def write_environment_audit_doc(path: Path | None = None) -> Path:
    """Write the release audit reference and return its path."""
    target = path or DOC_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_environment_audit_doc(), encoding="utf-8")
    return target


def case_keys(version: str = "0.2.0") -> list[tuple]:
    return [identity_from_row(row).key() for row in release_cases(version)]
