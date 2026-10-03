"""Render the release benchmark-audit reference.

The page lists every case in release 0.2.0. A case the full audit did not
execute stays ``not_run``. That status is not a pass.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from experiment.audit.coverage import cover_release, gap_count, release_cases
from experiment.audit.environment import CaseAudit, identity_from_row
from experiment.audit.provenance import provenance_complete
from experiment.audit.live import declared_probe

DOC_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "benchmarks" / "benchmark-audit.md"
)
RESULTS_DIR = DOC_PATH.parents[2] / "runtime" / "environment-audit-results"


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


def render_benchmark_audit_doc(
    *,
    version: str = "0.2.0",
    audits: list[CaseAudit] | None = None,
    records: list[dict] | None = None,
) -> str:
    """Return the reference page for release ``version``."""
    stored = load_stored_records() if records is None and audits is None else records
    stored = [
        item
        for item in (stored or [])
        if provenance_complete(CaseAudit.model_validate(item["audit"]).provenance)
    ]
    if audits is None:
        audits = [CaseAudit.model_validate(item["audit"]) for item in (stored or [])]
    rows = release_cases(version)
    release_keys = {identity_from_row(row).key() for row in rows}
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
        "# Benchmark audit",
        "",
        f"The benchmark audit deploys every case in benchmark release {version} in a real NIKA lab, injects the fault, and checks that the fault produces its declared network symptom and stays in place.",
        "Use this page to check whether a release case is valid before you score agents on it, and to rerun the audit after you change a scenario, fault, or symptom check.",
        "",
        "A case is one scenario, scale, backend, design options, fault, and inject parameters.",
        "Release cases come from `benchmark/releases/<version>/dev.yaml` and `test.yaml`.",
        "",
        "## Result",
        "",
        f"Release {version} has {counts['cases']} cases ({_split_counts(covered)}).",
        f"Admitted cases: {counts['admitted']}.",
        "",
        "| Status | Cases |",
        "| --- | --- |",
    ]
    for status in ("pass", "fail", "skipped", "unsupported", "no_evidence", "not_run"):
        lines.append(f"| `{status}` | {counts[status]} |")
    lines.extend(
        [
            "",
            "Each case declares one symptom probe. The probe decides what the audit measures after inject.",
            "",
            "| Probe | Cases |",
            "| --- | --- |",
        ]
    )
    for probe, count in sorted(probes.items()):
        label = probe or "healthy"
        lines.append(f"| `{label}` | {count} |")
    lines.extend(_environment_section(stored or []))
    lines.extend(_procedure_section())
    lines.extend(_run_section())
    lines.extend(_limits_section())
    lines.extend(
        _executed_section(
            [
                item
                for item in (stored or [])
                if identity_from_row(item["audit"]["identity"]).key() in release_keys
                and (item["audit"].get("symptom_probe") or "")
                == (
                    ""
                    if item["audit"]["identity"].get("fault") == "healthy"
                    else declared_probe(item["audit"]["identity"]["fault"])
                )
            ]
        )
    )
    lines.extend(
        [
            "",
            "## Cases",
            "",
            "Every release case, grouped by scenario. The Admission column comes from the matching stored run.",
            "",
        ]
    )
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
    from experiment.audit.matrix import audit_plan, result_current

    pending: list[dict] = []
    for row in audit_plan():
        if result_current(row):
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
    from experiment.audit.matrix import diagnose

    lines = [
        "",
        "## Executed audits",
        "",
        "Each row is the stored result of one `audit_case` run.",
        "The case tables in [Cases](#cases) count a run only when its scenario, scale, backend, design options, fault, and inject parameters all match a release case.",
        "",
        "The Diagnosis column is `pass` for a passing run.",
        "Otherwise it starts with `verify:` when the check or a host prerequisite failed, or with `case:` when the fault symptom did not appear on that lab.",
        "",
    ]
    if not records:
        lines.append("No current live audit result is stored yet.")
        lines.append("")
        return lines
    lines.extend(
        [
            "| Scenario | Fault | Scale | Backend | Design | Inject | Admission | Diagnosis |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    parsed = []
    for item in records:
        audit = CaseAudit.model_validate(item["audit"])
        parsed.append(
            (audit, diagnose(audit, str(item["error"]) if item.get("error") else None))
        )
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
    "verify: the behavioral stages passed and this fault has no separate control path",
    "verify: a recorded stage has no behavioral check",
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
    if not pending:
        return []
    lines = [
        "### Cases without a stored result",
        "",
        "The matrix schedules these cases by resource class and host capacity.",
        "",
    ]
    lines.extend(
        [
            "| Resource | Scenario | Fault |",
            "| --- | --- | --- |",
        ]
    )
    from experiment.audit.matrix import effective_class

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


_HOST = (
    ("OS", "Ubuntu 24.04.5 LTS, Linux 6.8.0-142-generic"),
    ("CPU", "16 vCPU"),
    ("Memory", "62 GiB"),
    ("Docker Engine", "29.8.1"),
    ("Containerlab", "0.79.0"),
    ("Kathara", "3.8.3"),
    ("Python", "3.12.12, run through `uv`"),
)


def _environment_section(records: list[dict]) -> list[str]:
    lines = [
        "",
        "## Test environment",
        "",
        "The release 0.2.0 audit ran on one host. Other NIKA workloads shared that host, so the matrix admits a case only when enough memory is free (see [Launch parameters](#launch-parameters)).",
        "",
        "| Component | Version |",
        "| --- | --- |",
    ]
    lines.extend(f"| {name} | {value} |" for name, value in _HOST)
    if not records:
        return lines
    provenances = [item["audit"]["provenance"] for item in records]
    started = min(p["started_at"] for p in provenances)[:16].replace("T", " ")
    completed = max(p["completed_at"] for p in provenances)[:16].replace("T", " ")
    elapsed = sorted(float(item.get("elapsed_sec") or 0) for item in records)
    lines.extend(
        [
            "",
            f"The audit ran from {started} to {completed} UTC.",
            f"Each case took {elapsed[len(elapsed) // 2] / 60:.1f} minutes at the median and {elapsed[-1] / 60:.1f} minutes at most, from lab deploy to undeploy.",
            "",
            "Lab nodes used these images. Every node that used an image reference had the same image ID.",
            "",
            "| Image | Image ID |",
            "| --- | --- |",
        ]
    )
    images: dict[str, set[str]] = {}
    for provenance in provenances:
        for image in provenance["images"].values():
            images.setdefault(str(image.get("reference")), set()).add(
                str(image.get("image_id", ""))[7:19]
            )
    for reference, ids in sorted(images.items()):
        lines.append(f"| `{reference}` | {', '.join(f'`{i}`' for i in sorted(ids))} |")
    return lines


def _procedure_section() -> list[str]:
    return [
        "",
        "## What the audit checks for one case",
        "",
        "`audit_case` in `experiment/audit/live.py` audits one case in this order:",
        "",
        "1. Deploy the lab with `start_net_env`, using the case scenario, scale, backend, and design options.",
        "2. Run the scenario health checks (`verify_lab`). This is stage `baseline_lab`.",
        "3. Probe the fault path while the lab is healthy. This is stage `baseline_path`. It shows that the symptom probe sees a working path before inject.",
        "4. Inject the fault with the case inject parameters and run `verify_fault`. This is stage `inject_artifact`.",
        "5. Run the declared symptom probe (stage `symptom`) and a control path that avoids the root cause (stage `control_path`).",
        "6. Wait for the persistence window, then read the fault artifact and the symptom again (stages `persistence_artifact` and `persistence_symptom`).",
        "7. Read the artifact and the symptom once more (stages `final_artifact` and `final_symptom`).",
        "8. Undeploy the lab with `close_session`, even when a stage fails.",
        "",
        "A `healthy` case runs `verify_lab` before and after a 2-second window and skips steps 3 to 7.",
        "",
        "The control path starts from a host that is not a root-cause node, a host behind a root-cause interface or link, or an attacker or load generator named in the inject parameters.",
        "When no such path exists, the stage records `no_control_path`. That status does not block admission. A control path that exists and fails does block admission.",
        "",
        "A stage with an empty observation is `no_evidence`. A stage whose observation reports an error, such as a command timeout, is `fail`. Neither status admits a case.",
        "",
        "### Admission statuses",
        "",
        "The audit admits a case only when its admission status is `pass`.",
        "",
        "| Status | Meaning |",
        "| --- | --- |",
        "| `pass` | Every required stage observed the expected condition. A missing control path does not block `pass`. |",
        "| `fail` | A stage ran and the observation failed. |",
        "| `skipped` | The audit ran and skipped the stage. |",
        "| `unsupported` | The fault has no behavioral check for the stage. `reason` names the scope. |",
        "| `no_evidence` | The stage produced no observation. |",
        "| `not_run` | No complete stored result exists for this case. |",
        "",
        "A stored result counts only when it has a finished run, a Git commit, image identities for every lab node, and the symptom probe that the fault declares today. Otherwise the case shows `not_run`.",
    ]


def _run_section() -> list[str]:
    from experiment.audit.live import _DYNAMIC_WINDOW_SEC
    from experiment.audit.matrix import (
        _ADMISSION_SPACING_SEC,
        _CLASS_SLOTS,
        _MEMORY_FLOOR_GIB,
    )

    lines = [
        "",
        "## Launch parameters",
        "",
        "Run every command from the repository root on a host with Docker and the NIKA images built.",
        "",
        "The release 0.2.0 audit used these commands:",
        "",
        "```shell",
        "# Audit every release case and replace stored results",
        "uv run python -m experiment.audit.matrix --jobs 8 --force",
        "",
        "# Resume after an interruption. Cases with a stored result are skipped.",
        "uv run python -m experiment.audit.matrix --jobs 8",
        "",
        "# Rerun one scenario and fault after a fix. Other results stay valid.",
        "uv run python -m experiment.audit.matrix --jobs 2 --scenario campus_lan --fault dns_lookup_latency --force",
        "```",
        "",
        "| Option | Default | Effect |",
        "| --- | --- | --- |",
        "| `--jobs N` | `2` | Number of worker processes. The scheduler below still limits how many labs run at once. |",
        "| `--force` | off | Rerun every selected case, even when it has a stored result. |",
        "| `--retry-failed` | off | Also rerun selected cases whose stored result is not `pass`. |",
        "| `--scenario NAME` | all | Select cases of one scenario. |",
        "| `--fault NAME` | all | Select cases of one fault. |",
        "| `--resource-class CLASS` | all | Select `light`, `large`, `k8s`, or `clab` cases. |",
        "",
        "The command exits with status 1 when any selected case lacks a passing result.",
        "",
        "### Scheduler",
        "",
        "Each case belongs to one resource class. `clab` is a Containerlab backend, `k8s` is a Kubernetes or llm-d scenario, `large` is topo size `l`, and `light` is everything else.",
        "Before a case deploys, the matrix waits until available memory is at or above the class floor and the 1-minute load average is below twice the CPU count. It then waits the class spacing so the next check sees the memory the new lab claims.",
        "",
        "| Class | Concurrent labs | Free memory floor | Spacing after admission |",
        "| --- | --- | --- | --- |",
    ]
    for name in ("light", "large", "k8s", "clab"):
        lines.append(
            f"| `{name}` | {_CLASS_SLOTS[name]} | {_MEMORY_FLOOR_GIB[name]} GiB | {_ADMISSION_SPACING_SEC[name]} s |"
        )
    lines.extend(
        [
            "",
            "### Persistence window",
            "",
            "Static faults wait 2 seconds between the symptom read and the persistence read. Dynamic faults wait longer:",
            "",
            "| Fault | Window |",
            "| --- | --- |",
        ]
    )
    for fault, seconds in sorted(_DYNAMIC_WINDOW_SEC.items()):
        lines.append(f"| `{fault}` | {seconds:g} s |")
    lines.extend(
        [
            "",
            "### Results and this page",
            "",
            "The matrix writes one JSON record per case to `runtime/environment-audit-results/`. Each record holds the case identity, every stage with its evidence, the diagnosis, the elapsed time, and the provenance: Git commit and dirty flag, source and configuration hashes, start and finish timestamps, session ID, and image ID and repository digests for each lab node.",
            "",
            "Regenerate this page from the stored records:",
            "",
            "```shell",
            'uv run python -c "from experiment.audit.report_doc import write_benchmark_audit_doc; write_benchmark_audit_doc()"',
            "```",
        ]
    )
    return lines


def _limits_section() -> list[str]:
    return [
        "",
        "## Limits",
        "",
        "- The persistence window lasts seconds. It does not cover the 2400-second trial budget. In this release, the SYN flood, incast, and sender and receiver contention cases set `duration=3600`, and the load balancer overload workers run until recovery. The benchmark trial also checks the artifact while the agent runs (see [Checks during a benchmark trial](#checks-during-a-benchmark-trial)).",
        "- The P4 gateway ECN probe counts ECN marks from a virtual queue that drains about 61 packets per second. It does not measure congestion of a physical egress queue. The `queue_occupancy` register in that scenario reports the modeled queue depth.",
        "",
        "## Checks during a benchmark trial",
        "",
        "`nika benchmark run` does not run this audit. Each trial runs lighter checks:",
        "",
        "1. `startup_verify_lab` after the lab starts.",
        "2. `verify_fault` after inject.",
        "3. `PresenceWatch` reads the fault artifact on the problem instance that injected it: once 2 seconds after the agent starts, and once before NIKA removes the lab. For the faults in `DYNAMIC_ARTIFACT_FAULTS`, the read checks the live worker, flap, or quota.",
        "",
        "The trial writes those reads to `fault-presence.json`. A `present` read means the artifact was still on the lab. It does not measure the network effect.",
        "When any of these reads finds the artifact absent or fails, the trial outcome is `environment_invalid`. Leaderboard averages omit that outcome, and `nika benchmark run --resume` deletes the slot and runs it again.",
        "When the agent run is interrupted (Ctrl+C or SIGTERM), NIKA skips the remaining reads and undeploys the lab.",
    ]


def write_benchmark_audit_doc(path: Path | None = None) -> Path:
    """Write the release audit reference and return its path."""
    target = path or DOC_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_benchmark_audit_doc(), encoding="utf-8")
    return target
