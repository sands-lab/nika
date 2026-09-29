"""Render the release environment-audit reference.

The page lists every case in release 0.2.0. A case the full audit did not
execute stays ``not_run``. That status is not a pass.
"""

from __future__ import annotations

import json
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
RESULTS_DIR = DOC_PATH.parent / "environment-audit-results"


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


def load_stored_records() -> list[dict]:
    """Return stored full-audit records. Missing files yield an empty list."""
    if not RESULTS_DIR.is_dir():
        return []
    records: list[dict] = []
    for path in sorted(RESULTS_DIR.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("audit"), dict):
            records.append(payload)
    return records


def render_environment_audit_doc(
    *,
    version: str = "0.2.0",
    audits: list[CaseAudit] | None = None,
    records: list[dict] | None = None,
) -> str:
    """Return the reference page for release ``version``."""
    stored = load_stored_records() if records is None and audits is None else records
    if audits is None:
        audits = [CaseAudit.model_validate(item["audit"]) for item in (stored or [])]
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
    lines.extend(_executed_section(stored or []))
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


def _pending_rows() -> list[dict]:
    from nika.net_env.net_env_pool import _NET_ENV_SPECS
    from tests.audit.matrix import audit_plan, result_path

    pending: list[dict] = []
    for row in audit_plan():
        if result_path(row).is_file():
            continue
        spec = _NET_ENV_SPECS.get(str(row.get("scenario") or ""))
        module = getattr(spec, "module", "")
        # Pytest registers lab doubles such as simple_bgp. The audit page
        # lists production scenarios only.
        if str(module).startswith("tests."):
            continue
        pending.append(row)
    return pending


def _executed_section(records: list[dict]) -> list[str]:
    lines = [
        "",
        "## Executed audits",
        "",
        "Each row is one live `audit_case` run: one healthy lab per scenario, and one fault case per failure.",
        "The release table below changes only when that run has the same scenario, scale, backend, design, fault, and inject parameters.",
        "",
        "A diagnosis that starts with `verify` names the check or the host prerequisite.",
        "A diagnosis that starts with `case` names the fault symptom on that lab.",
        "",
    ]
    if not records:
        lines.append("No live audit result is stored yet.")
        lines.append("")
        return lines
    lines.extend(
        [
            "| Scenario | Fault | Scale | Backend | Design | Inject | Admission | Diagnosis |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    parsed = [
        (
            CaseAudit.model_validate(item["audit"]),
            str(item.get("diagnosis") or ""),
        )
        for item in records
    ]
    parsed.sort(key=lambda item: (item[0].identity.scenario, item[0].identity.fault))
    for audit, diagnosis in parsed:
        identity = audit.identity
        lines.append(
            "| "
            + " | ".join(
                _cell(part)
                for part in (
                    identity.scenario,
                    identity.fault,
                    identity.topo_size or "none",
                    identity.backend or "scenario default",
                    _design(identity),
                    _inject(identity),
                    audit.admission(),
                    diagnosis,
                )
            )
            + " |"
        )
    lines.append("")
    lines.extend(_closer_look(parsed))
    lines.extend(_pending_section())
    return lines


_GENERIC_DIAGNOSIS = {
    "pass",
    "verify: the symptom contract is artifact_only, so this run has no network-effect observation",
    "verify: the behavioral stages passed and this fault has no separate control path",
    "verify: a recorded stage has no behavioral check",
    "verify: the symptom contract is control_plane_only, so this run has no data-plane observation",
}


def _closer_look(parsed: list[tuple[CaseAudit, str]]) -> list[str]:
    rows = [
        (audit, diagnosis)
        for audit, diagnosis in parsed
        if diagnosis not in _GENERIC_DIAGNOSIS
    ]
    if not rows:
        return []
    lines = [
        "### Rows to inspect",
        "",
        "These runs left a failed stage or a host prerequisite. The diagnosis says whether that came from the fault or from the check.",
        "",
        "| Scenario | Fault | Admission | Diagnosis |",
        "| --- | --- | --- | --- |",
    ]
    for audit, diagnosis in rows:
        identity = audit.identity
        lines.append(
            "| "
            + " | ".join(
                _cell(part)
                for part in (
                    identity.scenario,
                    identity.fault,
                    audit.admission(),
                    diagnosis,
                )
            )
            + " |"
        )
    lines.append("")
    return lines


def _pending_section() -> list[str]:
    pending = _pending_rows()
    lines = [
        "### Audits still waiting",
        "",
    ]
    if not pending:
        lines.append("Every planned scenario and failure has a stored result.")
        lines.append("")
        return lines
    lines.append(
        "These rows are exclusive labs. The runner starts one when the host has no other containers."
    )
    lines.append("")
    lines.extend(
        [
            "| Resource | Scenario | Fault |",
            "| --- | --- | --- |",
        ]
    )
    from tests.audit.matrix import effective_class

    for row in pending:
        resource = _cell(effective_class(row))
        scenario = _cell(str(row.get("scenario") or ""))
        fault = _cell(str(row.get("problem") or ""))
        lines.append(f"| {resource} | {scenario} | {fault} |")
    lines.append("")
    return lines


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
