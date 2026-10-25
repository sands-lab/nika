"""Audit one submission end to end: audit, scan, group, adjudicate, curate.

1. The deterministic audit runs the knowledge base (built-in checks and
   learned issues) over every trial.
2. The scan asks every trial the issue-class questions (LLM, parallel).
3. Findings are grouped across trials.
4. Groups a learned issue already fires on, or a built-in check whose
   signature identifies the instance (a state-changing command, a tool
   outside the policy), are recorded; the rest are adjudicated (LLM, one
   call per group).
5. Groups judged real become issue files (deterministic curation), unless a
   recorded issue's text signature already matches their evidence.
6. Cross-trial issues: per-environment tables of cases, healthy tasks, and
   tools are read with the same questions; real findings become issues with
   statistical signatures (``auditlib.cohort_audit``).

Every intermediate result is written to *workdir*, so a step can be inspected
or resumed. The issue files land in ``<kb_dir>/issues`` for a maintainer to
review.

A group's verdict does not depend on the memory, and neither does the scan
when its packets carry only the built-in checks' hits. An experiment replaying
many submissions can therefore compute both for all of them in parallel first
and pass them in as *precomputed*; only the memory-dependent steps (recorded
check and curation) then run in order. Scans and verdicts with failed LLM
calls are not kept, so a rerun retries them.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

from auditlib.adjudicate import adjudicate_groups
from auditlib.aggregate import group_findings
from auditlib.cohort_audit import build_rows, judge, scan_rows
from auditlib.cohort_audit import curate as curate_cohort
from auditlib.curate import covered_by_signature, curate_group, recorded_by
from auditlib.kb import load_kb
from auditlib.runner import audit_trajectories, write_report
from auditlib.scan import ScanConfig, scan_trials
from auditlib.trace import load_trial, trial_dirs


def _align(verdicts: list, results: list) -> list:
    """Curation results line up with the findings judged real, in order."""
    it = iter(results)
    return [next(it) if v["verdict"] == "real" else None for v in verdicts]


# Signature kinds that identify one instance completely (a state-changing
# command, a tool outside the policy, a validity flag). A built-in check of
# these kinds records a group at its step; text checks only find a shape, so a
# group they fire on may still be a specific issue worth its own file.
_INSTANCE_KINDS = {
    "mutation",
    "tool_policy",
    "validity",
    "regrade",
    "fingerprint_drift",
}


def _exact(issue) -> bool:
    return any(s.kind in _INSTANCE_KINDS for s in issue.detect)


def worth_judging(group: dict[str, Any], samples: int) -> bool:
    """Adjudicate a group seen in two or more trials, or by two or more scans.

    A single scan of a single trial is mostly noise; on the labelled
    submissions this rule kept 40% of the groups and nearly all the reference
    issues they reached.
    """
    seen_by = group.get("sample_agreement", 0.0) * max(1, samples)
    # sample_agreement is rounded to two decimals.
    return group.get("trials", 0) >= 2 or seen_by >= 2 - 0.05


def _verdicts(path, groups, todo, trials, kb, cfg) -> list[dict[str, Any]]:
    """Verdicts for the groups in *todo*, cached in *path* by group content."""

    def key(i: int) -> str:
        g = groups[i]
        return json.dumps([g["class"], g["claim"], g["all_refs"]])

    cached: dict[str, dict] = {}
    if path.is_file():
        cached = json.loads(path.read_text(encoding="utf-8"))
    missing = [i for i in todo if key(i) not in cached]
    judged = adjudicate_groups([groups[i] for i in missing], trials, kb, cfg)
    for i, v in zip(missing, judged):
        if not v["error"]:
            cached[key(i)] = v
    path.write_text(json.dumps(cached) + "\n", encoding="utf-8")
    fresh = dict(zip(missing, judged))
    return [cached.get(key(i)) or fresh[i] for i in todo]


def review_submission(
    pkg: Path,
    kb_dir: Path,
    workdir: Path,
    *,
    scan: ScanConfig | None,
    adjudicate: ScanConfig | None,
    history: list | None = None,
    repo: Path | None = None,
    records_out: list | None = None,
    precomputed: dict[str, list] | None = None,
) -> dict[str, Any]:
    """*precomputed*, when given, holds ``records`` (scan output) and
    ``verdicts`` (one per group, in ``group_findings`` order)."""
    workdir.mkdir(parents=True, exist_ok=True)
    timings: dict[str, float] = {}
    t0 = time.time()
    kb = load_kb(kb_dir)
    current: list = []
    report = audit_trajectories(
        pkg, kb=kb, history=list(history or []), records_out=current, repo=repo
    )
    if records_out is not None:
        records_out.extend(current)
    write_report(report, workdir / "audit.json")
    timings["audit"] = round(time.time() - t0, 1)

    t0 = time.time()
    scan_path = workdir / "scan.jsonl"
    if precomputed is not None:
        records = precomputed["records"]
    elif scan_path.is_file():
        records = [json.loads(line) for line in scan_path.read_text().splitlines()]
    else:
        records = scan_trials(trial_dirs(pkg), kb, scan, hits=report.get("trials", {}))
        if not any(r["errors"] for r in records):
            scan_path.write_text("".join(json.dumps(r) + "\n" for r in records))
    timings["scan"] = round(time.time() - t0, 1)

    trials = {p.name: load_trial(p) for p in trial_dirs(pkg)}
    groups = group_findings(records, trials)
    learned = {
        i.id: i.issue_class
        for i in kb.issues
        if (kb_dir / "issues" / f"{i.id}.yaml").is_file()
    }
    learned_issues = [i for i in kb.issues if i.id in learned]
    exact = {i.id: i.issue_class for i in kb.issues if _exact(i)}
    for g in groups:
        g["recorded_by"] = recorded_by(
            g, report.get("trials", {}), learned | exact, frozenset(exact)
        ) or covered_by_signature(g, learned_issues)
    samples = max((r.get("sample", 0) for r in records), default=0) + 1
    todo = [
        i
        for i, g in enumerate(groups)
        if not g["recorded_by"] and worth_judging(g, samples)
    ]

    t0 = time.time()
    if precomputed is not None:
        verdicts = [precomputed["verdicts"][i] for i in todo]
    else:
        verdicts = _verdicts(
            workdir / "verdicts.json", groups, todo, trials, kb, adjudicate
        )
    for i, v in zip(todo, verdicts, strict=True):
        groups[i]["verdict"] = v["verdict"]
        groups[i]["verdict_reason"] = v["reason"]
    timings["adjudicate"] = round(time.time() - t0, 1)

    t0 = time.time()
    written = []
    for g in groups:
        if g.get("verdict") != "real":
            continue
        fresh = load_kb(kb_dir)
        # Issues recorded before this submission or for an earlier group of it.
        same = covered_by_signature(
            g,
            [i for i in fresh.issues if (kb_dir / "issues" / f"{i.id}.yaml").is_file()],
        )
        if same:
            g["curation"] = {"written": None, "recorded_by": same}
            continue
        result = curate_group(
            g,
            trials,
            fresh,
            kb_dir,
            source=f"submission audit of {pkg.name}",
            date=date.today().isoformat(),
        )
        g["curation"] = result
        if result["written"]:
            written.append(result["written"])
    timings["curate"] = round(time.time() - t0, 1)
    (workdir / "groups.json").write_text(json.dumps(groups, indent=1) + "\n")

    t0 = time.time()
    cohort = {"findings": 0, "real": 0, "written": []}
    if adjudicate is not None:
        rows = build_rows(current, list(history or []), report)
        # Like the trial scan, the table scan only reports; the judge reasons.
        found = scan_rows(
            rows, kb, replace(adjudicate, thinking=False, max_tokens=8192)
        )
        verdicts_c = judge(found, rows, kb, adjudicate)
        results = curate_cohort(
            found,
            verdicts_c,
            rows,
            load_kb(kb_dir),
            kb_dir,
            current,
            list(history or []),
            source=f"submission audit of {pkg.name}",
            date=date.today().isoformat(),
        )
        (workdir / "cohort.json").write_text(
            json.dumps(
                [
                    {**f, **v, "curation": r}
                    for f, v, r in zip(found, verdicts_c, _align(verdicts_c, results))
                ],
                indent=1,
            )
            + "\n"
        )
        cohort = {
            "findings": len(found),
            "real": sum(v["verdict"] == "real" for v in verdicts_c),
            "written": [r["written"] for r in results if r.get("written")],
        }
        written += cohort["written"]
    timings["cohort"] = round(time.time() - t0, 1)
    findings = [f for r in records for f in r["findings"]]
    return {
        "submission": pkg.name,
        "trials": len(trials),
        "findings": len(findings),
        "supported": sum(f["citation_status"] == "supported" for f in findings),
        "groups": len(groups),
        "recorded": sum(bool(g["recorded_by"]) for g in groups),
        "adjudicated": len(todo),
        "real": sum(g.get("verdict") == "real" for g in groups),
        "written": written,
        "cohort": cohort,
        # Failed LLM calls: trial scans and adjudications.
        "errors": [
            sum(bool(r["errors"]) for r in records),
            sum(bool(v.get("error")) for v in verdicts),
        ],
        "not_written": sum(
            1 for g in groups if g.get("curation") and not g["curation"]["written"]
        ),
        "scan_tokens": [
            sum(r["prompt_tokens"] for r in records),
            sum(r["completion_tokens"] for r in records),
        ],
        "adjudicate_tokens": [
            sum(v["tokens"][0] for v in verdicts),
            sum(v["tokens"][1] for v in verdicts),
        ],
        "timings": timings,
    }
