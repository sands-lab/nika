"""Run the deterministic audit pipeline (Match, Scan, Adjudicate, record) on one run.

Same steps as ``nika.workflows.leaderboard.review.review_trajectories``, with three
experiment changes applied by patching, not by editing the library:

* Scan packets carry only the built-in checks' hits, so a trial's scan does not
  depend on the learned knowledge base and every arm and step shares one scan.
* Scan and adjudication replies are cached across arms and steps, keyed by the
  request content without the gateway alias (every alias serves one recipe).
* Each request carries ``X-Gateway-Session`` derived from its prompt, so the three
  samples of a trial reach the same replica and reuse its prefix cache.

The work directory may already exist, so an interrupted review resumes.
Run with the sandbox tree's Python.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from dataclasses import replace
from pathlib import Path

import httpx

import nika.audit.pipeline as pipeline
from nika.audit.adjudicate import adjudicate_groups, group_prompt
from nika.audit.adjudicate import system_prompt as judge_prompt
from nika.audit.cohort import history_from_scores
from nika.audit.kb import load_kb
from nika.audit.runner import audit_trajectories, write_report
from nika.audit.scan import ScanConfig, chunks, scan_trials, system_prompt
from nika.audit.trace import load_trial

ROOT = Path(os.environ.get("AUDIT_EVO_ROOT", "/workspace/nika-audit-evolution"))
CACHE = ROOT / "cache" / os.environ.get("AUDIT_EVO_MODEL", "qwen3.8")
EMPTY_KB = ROOT / "sandbox" / "tree" / "src" / "nika" / "audit" / "kb_data"
BUILTIN = {i.id for i in load_kb(EMPTY_KB).issues}


def _get(kind: str, key: str):
    f = CACHE / kind / key[:2] / f"{key}.json"
    return json.loads(f.read_text()) if f.exists() else None


def _put(kind: str, key: str, value) -> None:
    f = CACHE / kind / key[:2] / f"{key}.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value))
    tmp.replace(f)


def _key(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()


def builtin_hits(hits: dict | None) -> dict:
    return {
        t: [h for h in hs if h.get("issue") in BUILTIN]
        for t, hs in (hits or {}).items()
    }


def shared_scan_trials(paths, kb, cfg, *, hits=None, on_result=None, done=None):
    """scan_trials with built-in hits only and a cache shared by all arms and steps."""
    hits, done = builtin_hits(hits), set(done or ())
    system = system_prompt(kb)
    keys, reused = {}, []
    for path in paths:
        trial = load_trial(path)
        pieces = chunks(trial, hits.get(trial.trial_id, []), cfg.chunk_chars)
        for s in range(cfg.samples):
            if (trial.trial_id, s) in done:
                continue
            k = _key(
                system,
                cfg.temperature,
                cfg.max_tokens,
                cfg.thinking,
                cfg.chunk_chars,
                trial.trial_id,
                pieces,
                s,
            )
            keys[(trial.trial_id, s)] = k
            if (rec := _get("scan", k)) is not None:
                rec = {**rec, "cached": True}
                reused.append(rec)
                done.add((trial.trial_id, s))
                if on_result:
                    on_result(rec)

    def store(rec):
        k = keys.get((rec["trial"], rec["sample"]))
        if k and not rec["errors"]:
            _put("scan", k, rec)
        if on_result:
            on_result(rec)

    fresh = scan_trials(paths, kb, cfg, hits=hits, on_result=store, done=done)
    print(f"Shared scan cache: {len(reused)} reused, {len(fresh)} scanned.", flush=True)
    return reused + fresh


def shared_adjudicate_groups(groups, trials, kb, cfg, *, on_result=None):
    """adjudicate_groups with a cache shared by all arms and steps."""
    system = judge_prompt(kb)
    keys = [
        _key(
            system,
            group_prompt(g, trials),
            cfg.thinking,
            cfg.temperature,
            cfg.max_tokens,
        )
        for g in groups
    ]
    results: list = [None] * len(groups)
    todo = []
    for i, k in enumerate(keys):
        if (v := _get("verdict", k)) is not None:
            v = {**v, "cached": True}
            results[i] = v
            if on_result:
                on_result(i, v)
        else:
            todo.append(i)

    def store(j, v):
        i = todo[j]
        if not v.get("error"):
            _put("verdict", keys[i], v)
        if on_result:
            on_result(i, v)

    for j, v in enumerate(
        adjudicate_groups([groups[i] for i in todo], trials, kb, cfg, on_result=store)
    ):
        results[todo[j]] = v
    print(
        f"Shared verdict cache: {len(groups) - len(todo)} reused, {len(todo)} judged.",
        flush=True,
    )
    return results


def _adapt(url, kwargs) -> None:
    """Route a trial's samples to one cache, and fit the payload to the endpoint.

    The OpenAI API rejects vLLM's ``chat_template_kwargs``, ``max_tokens`` for
    reasoning models, and non-default temperatures; reasoning is set by effort.
    """
    payload = kwargs.get("json") or {}
    msgs = payload.get("messages") or []
    if not msgs:
        return
    sid = (
        "audit-evo-" + hashlib.sha1(json.dumps(msgs)[:20000].encode()).hexdigest()[:24]
    )
    if "api.openai.com" in str(url):
        p = dict(payload)
        thinking = (p.pop("chat_template_kwargs", None) or {}).get("enable_thinking")
        p["reasoning_effort"] = "medium" if thinking else "none"
        if "max_tokens" in p:
            p["max_completion_tokens"] = p.pop("max_tokens")
        p.pop("temperature", None)
        p["prompt_cache_key"] = sid
        if p.get("stream"):
            p["stream_options"] = {"include_usage": True}
        kwargs["json"] = p
    else:
        kwargs["headers"] = {**(kwargs.get("headers") or {}), "X-Gateway-Session": sid}


_post, _stream = httpx.Client.post, httpx.Client.stream


def _patched_post(self, url, **kwargs):
    _adapt(url, kwargs)
    return _post(self, url, **kwargs)


def _patched_stream(self, method, url, **kwargs):
    _adapt(url, kwargs)
    return _stream(self, method, url, **kwargs)


httpx.Client.post = _patched_post
httpx.Client.stream = _patched_stream
_covered_by_signature = pipeline.covered_by_signature


def same_class_covered_by_signature(group, issues, trials, **kwargs):
    """A learned signature records a group only if the issue has the group's class.

    Without this, a signature of any class firing on one cited step absorbs the
    group, and a different defect in the same steps is never judged.
    """
    same = [i for i in issues if i.issue_class == group.get("class")]
    return _covered_by_signature(group, same, trials, **kwargs)


pipeline.scan_trials = shared_scan_trials
pipeline.adjudicate_groups = shared_adjudicate_groups
pipeline.covered_by_signature = same_class_covered_by_signature


def scan_config(samples: int, workers: int) -> ScanConfig:
    return ScanConfig(
        base_url=os.environ["NIKA_AUDIT_LLM_URL"],
        model=os.environ["NIKA_AUDIT_LLM_MODEL"],
        api_key=os.environ.get("NIKA_AUDIT_LLM_KEY", "none"),
        samples=samples,
        workers=workers,
        thinking=False,
    )


def judge_config(scan: ScanConfig, workers: int) -> ScanConfig:
    """The adjudicator: same endpoint, reasoning on, decode-bound so more parallel."""
    return replace(scan, thinking=True, max_tokens=16384, workers=workers)


def prejudge(pkg: Path, samples: int, scan_workers: int, judge_workers: int) -> dict:
    """Phase A for one run: scan every trial and judge every group worth judging.

    Grouping depends only on the scans, so these verdicts cover every arm; an arm
    with memory judges a subset (groups its signatures already cover are skipped).
    """
    from nika.audit.aggregate import group_findings
    from nika.audit.pipeline import worth_judging
    from nika.audit.trace import trial_dirs

    kb = load_kb(EMPTY_KB)
    report = audit_trajectories(pkg, kb=kb)
    scan = scan_config(samples, scan_workers)
    records = shared_scan_trials(
        trial_dirs(pkg), kb, scan, hits=report.get("trials", {})
    )
    trials = {p.name: load_trial(p) for p in trial_dirs(pkg)}
    groups = [g for g in group_findings(records, trials) if worth_judging(g, samples)]
    verdicts = shared_adjudicate_groups(
        groups, trials, kb, judge_config(scan, judge_workers)
    )
    errs = [str(r["errors"])[:300] for r in records if r["errors"]]
    for e in errs[:5]:
        print("scan error:", e, flush=True)
    return {
        "scan_errors": len(errs),
        "groups": len(groups),
        "judge_errors": sum(bool(v.get("error")) for v in verdicts),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("trajectories", type=Path)
    ap.add_argument("--workdir", type=Path, required=True)
    ap.add_argument("--kb", type=Path, required=True)
    ap.add_argument("--repo", type=Path)
    ap.add_argument("--history", type=Path)
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--judge-workers", type=int, default=32)
    a = ap.parse_args()

    a.workdir.mkdir(parents=True, exist_ok=True)
    kb = a.workdir / "kb"
    if not kb.exists():
        shutil.copytree(a.kb, kb)
    past = history_from_scores(a.history, release="0.2.0") if a.history else []
    scan = scan_config(a.samples, a.workers)
    summary = pipeline.review_submission(
        a.trajectories,
        kb,
        a.workdir,
        scan=scan,
        adjudicate=judge_config(scan, a.judge_workers),
        history=past,
        repo=a.repo,
    )
    load_kb(kb)
    write_report(
        audit_trajectories(a.trajectories, kb=load_kb(kb), history=past, repo=a.repo),
        a.workdir / "audit-candidate.json",
    )
    (a.workdir / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
