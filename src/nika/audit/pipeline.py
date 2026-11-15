"""Audit one submission end to end: audit, scan, group, adjudicate, curate.

1. The deterministic audit runs the knowledge base (built-in checks and
   learned issues) over every trial.
2. The scan asks every trial the issue-class questions (LLM, parallel).
3. Findings are grouped across trials.
4. Groups whose excerpts a learned issue's signature matches (within the
   issue's scope), or that an issue identifying the instance (a state-changing
   command, a tool outside the policy) fires on at a cited step, are recorded; the rest are adjudicated (LLM, one
   call per group).
5. Groups judged real become issue files (deterministic curation), unless a
   recorded issue's text signature already matches their evidence.
6. Cross-trial issues: per-environment tables of cases, healthy tasks, and
   tools are read with the same questions; real findings become issues with
   statistical signatures (``nika.audit.cohort_audit``).

Every intermediate result is written to *workdir*, so a step can be inspected
or resumed. The issue files land in ``<kb_dir>/issues`` for a maintainer to
review.

A group's verdict does not depend on the memory, and neither does the scan
when its packets carry only the built-in checks' hits. An experiment replaying
many submissions can therefore compute both for all of them in parallel first
and pass them in as *precomputed*; only the memory-dependent steps (recorded
check and curation) then run in order. Failed model attempts remain in the
call journals; a rerun reuses successful results and retries failed work.
"""

from __future__ import annotations

import hashlib
import json
import time
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path
from threading import Lock
from typing import Any

from nika.audit.adjudicate import adjudicate_groups, group_prompt, system_prompt
from nika.audit.aggregate import group_findings
from nika.audit.cohort_audit import (
    build_rows,
    judge,
    prompt_rows,
    scan_rows,
)
from nika.audit.cohort_audit import curate as curate_cohort
from nika.audit.curate import covered_by_signature, curate_group, recorded_by
from nika.audit.curate_validate import validate_group
from nika.audit.contracts import audit_contracts
from nika.audit.brief import audit_briefs
from nika.audit.boundary import audit_boundaries
from nika.audit.capture import audit_captures
from nika.audit.normalization import audit_normalizations
from nika.audit.kb import load_kb
from nika.audit.runner import audit_trajectories, write_report
from nika.audit.scan import ScanConfig, chunks, scan_trials
from nika.audit.scan import system_prompt as scan_system_prompt
from nika.audit.trace import load_trial, trial_dirs


def _align(verdicts: list, results: list) -> list:
    """Curation results line up with the findings judged real, in order."""
    it = iter(results)
    return [next(it) if v["verdict"] == "real" else None for v in verdicts]


# Signature kinds that identify one instance at its step (a state-changing
# command, a tool outside the policy). An issue of these kinds records a group
# at that step; text checks only find a shape, so a group they fire on may
# still be a specific issue worth its own file.
_INSTANCE_KINDS = {"mutation", "tool_policy"}


def _exact(issue) -> bool:
    return any(s.kind in _INSTANCE_KINDS for s in issue.detect)


def worth_judging(group: dict[str, Any], samples: int) -> bool:
    """Adjudicate a group seen in two or more trials, or by two or more scans.

    A single scan of a single trial is mostly noise; on the labelled
    submissions this rule kept 40% of the groups and nearly all the reference
    issues they reached.
    """
    if group.get("confirmation") and group.get("counterfactuals"):
        return True
    seen_by = group.get("sample_agreement", 0.0) * max(1, samples)
    # sample_agreement is rounded to two decimals.
    return group.get("trials", 0) >= 2 or seen_by >= 2 - 0.05


def _verdicts(path, groups, todo, trials, kb, cfg) -> list[dict[str, Any]]:
    """Verdicts for the groups in *todo*, cached in *path* by group content."""

    def key(i: int) -> str:
        g = groups[i]
        packet = [
            cfg.model,
            cfg.command,
            cfg.base_url,
            cfg.thinking,
            cfg.temperature,
            cfg.max_tokens,
            system_prompt(kb),
            group_prompt(g, trials),
        ]
        return hashlib.sha256(json.dumps(packet).encode()).hexdigest()

    cached: dict[str, dict] = {}
    if path.is_file():
        cached = {
            k: v
            for k, v in json.loads(path.read_text(encoding="utf-8")).items()
            if isinstance(v.get("reason"), str)
            and v["reason"].strip()
            and not v.get("error")
        }
    missing = [i for i in todo if key(i) not in cached]

    def persist(index: int, verdict: dict) -> None:
        if index < len(missing):
            with path.with_name(path.stem + "-calls.jsonl").open(
                "a", encoding="utf-8"
            ) as calls:
                calls.write(
                    json.dumps({"key": key(missing[index]), "result": verdict}) + "\n"
                )
        if not verdict["error"]:
            cached[key(missing[index])] = verdict
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(cached) + "\n", encoding="utf-8")
        temporary.replace(path)

    judged = adjudicate_groups(
        [groups[i] for i in missing], trials, kb, cfg, on_result=persist
    )
    fresh = dict(zip(missing, judged))
    return [cached.get(key(i)) or fresh[i] for i in todo]


def _scans(
    path: Path, paths: list[Path], kb, cfg: ScanConfig, hits: dict
) -> list[dict]:
    """Retain scan attempts and retry only failed or interrupted matching work."""
    digest = hashlib.sha256(
        json.dumps(
            [
                scan_system_prompt(kb),
                cfg.command,
                cfg.base_url,
                cfg.model,
                cfg.temperature,
                cfg.max_tokens,
                cfg.thinking,
                cfg.chunk_chars,
            ]
        ).encode()
    )
    for trial_path in paths:
        trial = load_trial(trial_path)
        digest.update(
            json.dumps(
                [
                    trial.trial_id,
                    chunks(trial, hits.get(trial.trial_id, []), cfg.chunk_chars),
                ]
            ).encode()
        )
    identity = {"fingerprint": digest.hexdigest()}
    source = path.with_name(path.stem + "-source.json")
    calls = path.with_name(path.stem + "-calls.jsonl")
    try:
        matching = json.loads(source.read_text()) == identity
    except (FileNotFoundError, json.JSONDecodeError):
        matching = False
    if not matching:
        path.unlink(missing_ok=True)
        calls.write_text("")
        source.write_text(json.dumps(identity) + "\n")
    expected = [(p.name, s) for p in paths for s in range(cfg.samples)]
    expected_set = set(expected)
    records = {}

    def remember(record: dict) -> None:
        key = (record["trial"], record["sample"])
        previous = records.get(key)
        if previous and previous["errors"]:
            record = dict(record)
            for field in (
                "requests",
                "http_attempts",
                "prompt_tokens",
                "completion_tokens",
                "truncated",
                "sec",
            ):
                record[field] = (previous.get(field) or 0) + (record.get(field) or 0)
            record["sec"] = round(record["sec"], 1)
            record["failed_responses"] = previous.get(
                "failed_responses", []
            ) + record.get("failed_responses", [])
            record["recovered_errors"] = previous.get(
                "recovered_errors", []
            ) + record.get("recovered_errors", [])
            if not record["errors"]:
                record["recovered_errors"] += previous["errors"]
        records[key] = record

    if calls.is_file():
        lines = calls.read_text().splitlines(keepends=True)
        for index, line in enumerate(lines):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                if index == len(lines) - 1 and not line.endswith("\n"):
                    calls.write_text("".join(lines[:index]))
                    break
                raise
            key = (record["trial"], record["sample"])
            if key in expected_set:
                remember(record)
    done = {key for key, record in records.items() if not record["errors"]}
    for key in done:
        records[key] = {**records[key], "cached": True}
    lock = Lock()

    def persist(record: dict) -> None:
        with lock:
            with calls.open("a") as handle:
                handle.write(json.dumps(record) + "\n")
            print(
                f"Trial scan {record['trial']}, sample {record['sample'] + 1}: "
                f"{len(record['errors'])} errors.",
                file=sys.stderr,
                flush=True,
            )

    fresh = scan_trials(paths, kb, cfg, hits=hits, done=done, on_result=persist)
    for record in fresh:
        remember(record)
    result = [records[key] for key in expected]
    path.write_text("".join(json.dumps(r) + "\n" for r in result))
    return result


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
    ``verdicts`` (one per group, in ``group_findings`` order), and optionally
    ``cohort`` (the ``cohort.json`` of an earlier run of the same order)."""
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
    retried_scans = 0
    if precomputed is not None:
        records = precomputed["records"]
    else:
        records = _scans(scan_path, trial_dirs(pkg), kb, scan, report.get("trials", {}))
        if scan.command and scan.retries and any(r["errors"] for r in records):
            retried_scans = sum(bool(r["errors"]) for r in records)
            records = _scans(
                scan_path, trial_dirs(pkg), kb, scan, report.get("trials", {})
            )
    timings["scan"] = round(time.time() - t0, 1)

    trials = {p.name: load_trial(p) for p in trial_dirs(pkg)}
    groups = group_findings(records, trials)
    learned = [i for i in kb.issues if (kb_dir / "issues" / f"{i.id}.yaml").is_file()]
    automatic = None
    if repo is not None and adjudicate is not None:
        t0 = time.time()
        probe_work = workdir / "normalization"
        probes = audit_normalizations(
            groups, trials, kb, learned, repo, adjudicate, probe_work
        )
        calls = [
            call
            for record in probes["records"]
            if not record["image_review"].get("cached")
            for call in record["image_review"]["calls"]
        ]
        automatic = {
            "candidates": probes["candidates"],
            "applied": probes["confirmed_groups"],
            "errors": sum(bool(r.get("error")) for r in probes["records"]),
            "calls": len(calls),
            "tokens": [
                sum(c.get("prompt_tokens") or 0 for c in calls),
                sum(c.get("completion_tokens") or 0 for c in calls),
            ],
        }
        timings["normalization"] = round(time.time() - t0, 1)
    exact = {i.id: i.issue_class for i in kb.issues if _exact(i)}
    for g in groups:
        g["recorded_by"] = recorded_by(
            g, report.get("trials", {}), exact
        ) or covered_by_signature(g, learned, trials, kb=kb, repo=repo)
    samples = max((r.get("sample", 0) for r in records), default=0) + 1
    todo = [
        i
        for i, g in enumerate(groups)
        if not g["recorded_by"] and worth_judging(g, samples)
    ]

    t0 = time.time()
    if precomputed is not None:
        verdicts = [precomputed["verdicts"][i] for i in todo]
        confirmed = [j for j, i in enumerate(todo) if groups[i].get("confirmation")]
        if confirmed:
            if adjudicate is None:
                raise ValueError("controlled confirmations need an adjudicator")
            refreshed = _verdicts(
                workdir / "confirmation-verdicts.json",
                groups,
                [todo[j] for j in confirmed],
                trials,
                kb,
                adjudicate,
            )
            for j, verdict in zip(confirmed, refreshed, strict=True):
                verdicts[j] = verdict
    else:
        verdicts = _verdicts(
            workdir / "verdicts.json", groups, todo, trials, kb, adjudicate
        )
    for i, v in zip(todo, verdicts, strict=True):
        groups[i]["verdict"] = v["verdict"]
        groups[i]["verdict_reason"] = v["reason"]
        if groups[i].get("confirmation") and v["verdict"] == "real":
            groups[i]["original_claim"] = groups[i]["claim"]
            groups[i]["claim"] = v["reason"]
            groups[i]["other_claims"] = []
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
            trials,
            kb=fresh,
            repo=repo,
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
        if (
            adjudicate is not None
            and not result.get("written")
            and result.get("why", "").startswith("signature too generic:")
        ):
            prior = result
            result = validate_group(
                g,
                trials,
                fresh,
                kb_dir,
                workdir / "curation-validation",
                adjudicate,
                source=f"submission audit of {pkg.name}",
                date=date.today().isoformat(),
                precomputed=(precomputed or {}).get("validation"),
            )
            result["original_rejection"] = prior["why"]
        g["curation"] = result
        if result["written"]:
            written.append(result["written"])
    timings["curate"] = round(time.time() - t0, 1)
    (workdir / "groups.json").write_text(json.dumps(groups, indent=1) + "\n")

    t0 = time.time()
    cohort = {"findings": 0, "real": 0, "written": []}
    cohort_calls = []
    reuse = (precomputed or {}).get("cohort")
    if adjudicate is not None or reuse is not None:
        rows = build_rows(current, list(history or []), report) + prompt_rows(
            list(trials.values())
        )
        if reuse is not None:
            # Table findings and verdicts from an earlier run of the same
            # order; findings whose row changed are dropped.
            texts = {r.id: r.text for r in rows}
            # Failed judge calls are not reused, so their findings are dropped.
            kept = [
                c
                for c in reuse
                if not c.get("error")
                and isinstance(c.get("reason"), str)
                and c["reason"].strip()
                and c["excerpt"].strip() in texts.get(c["row"], "")
            ]
            found = [
                {k: c[k] for k in ("class", "row", "claim", "excerpt")} for c in kept
            ]
            verdicts_c = [
                {"verdict": c["verdict"], "reason": c.get("reason", "")} for c in kept
            ]
        else:
            # Like the trial scan, the table scan only reports; the judge reasons.
            found = scan_rows(
                rows,
                kb,
                replace(adjudicate, thinking=False, max_tokens=8192),
                calls_out=cohort_calls,
                cache_path=workdir / "cohort-scan-cache.json",
            )
            verdicts_c = judge(
                found,
                rows,
                kb,
                adjudicate,
                cache_path=workdir / "cohort-judge-cache.json",
            )
            (workdir / "cohort-scan.json").write_text(
                json.dumps(cohort_calls, indent=1) + "\n"
            )
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
            "scan_errors": sum(bool(c.get("error")) for c in cohort_calls),
            "judge_errors": sum(bool(v.get("error")) for v in verdicts_c),
            "cached_scans": sum(bool(c.get("cached")) for c in cohort_calls),
            "cached_judgments": sum(
                bool(v.get("response", {}).get("cached")) for v in verdicts_c
            ),
            "scan_tokens": [
                sum(c.get("prompt_tokens") or 0 for c in cohort_calls),
                sum(c.get("completion_tokens") or 0 for c in cohort_calls),
            ],
        }
        written += cohort["written"]
    timings["cohort"] = round(time.time() - t0, 1)
    contracts = None
    briefs = None
    boundaries = None
    captures = None
    if repo is not None:
        t0 = time.time()
        contracts = audit_contracts(
            list(trials.values()),
            repo,
            kb_dir,
            workdir / "contracts-cache.json",
            adjudicate,
            source=f"submission audit of {pkg.name}",
            date=date.today().isoformat(),
            precomputed=(precomputed or {}).get("contracts"),
        )
        written += contracts["written"]
        timings["contracts"] = round(time.time() - t0, 1)
        t0 = time.time()
        briefs = audit_briefs(
            list(trials.values()),
            repo,
            kb_dir,
            workdir / "briefs-cache.json",
            adjudicate,
            source=f"submission audit of {pkg.name}",
            date=date.today().isoformat(),
            precomputed=(precomputed or {}).get("briefs"),
        )
        written += briefs["written"]
        timings["briefs"] = round(time.time() - t0, 1)
        t0 = time.time()
        boundaries = audit_boundaries(
            list(trials.values()),
            repo,
            kb_dir,
            workdir / "boundaries-cache.json",
            adjudicate,
            source=f"submission audit of {pkg.name}",
            date=date.today().isoformat(),
            precomputed=(precomputed or {}).get("boundaries"),
        )
        written += boundaries["written"]
        timings["boundaries"] = round(time.time() - t0, 1)
        if adjudicate is not None:
            t0 = time.time()
            captures = audit_captures(
                list(trials.values()),
                repo,
                kb_dir,
                workdir / "captures",
                adjudicate,
                source=f"submission audit of {pkg.name}",
                date=date.today().isoformat(),
            )
            written += captures["written"]
            timings["captures"] = round(time.time() - t0, 1)
    findings = [f for r in records for f in r["findings"]]
    return {
        "submission": pkg.name,
        "trials": len(trials),
        "cached_scans": sum(bool(r.get("cached")) for r in records),
        "retried_scans": retried_scans,
        "findings": len(findings),
        "supported": sum(f["citation_status"] == "supported" for f in findings),
        "groups": len(groups),
        "recorded": sum(bool(g["recorded_by"]) for g in groups),
        "adjudicated": len(todo),
        "real": sum(g.get("verdict") == "real" for g in groups),
        "written": written,
        "cohort": cohort,
        "contracts": contracts,
        "briefs": briefs,
        "boundaries": boundaries,
        "captures": captures,
        "automatic_confirmations": automatic,
        "curation_validation": {
            "calls": sum(
                g.get("curation", {}).get("validation_calls", 0) for g in groups
            ),
            "errors": sum(
                bool(g.get("curation", {}).get("validation_error")) for g in groups
            ),
            "tokens": [
                sum(
                    g.get("curation", {}).get("validation_tokens", [0, 0])[i]
                    for g in groups
                )
                for i in range(2)
            ],
        },
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
