"""Score replayed audits against the ground truth.

A detection is a record (Ours, B1, B2) or finding (B3) together with the trials of
the audited run it covers. Its candidate ground-truth defects in that run are those
of the same class whose trials overlap it, plus same-class defects that have no
trial evidence ("run" source). An LLM judge decides for each candidate whether the
two describe the same defect; the detection credits at most one defect, the judged
match with the largest trial overlap. Judgements are cached and shared by all arms.

Detections that credit nothing are written to ``unmatched_<arm>_s<seed>.jsonl`` for
precision review. Output: ``<RESULTS>/<bench>/<arm>_s<seed>.json`` with, per step,
the defects detected in that run, the cumulative count, and new-record precision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

JUDGE_MODEL = "openai/gpt-6-luna"
JUDGE_CACHE = (
    C.ROOT / "cache" / "judge"
)  # shared across auditor models: it judges text only
JUDGE_SYSTEM = (
    "You compare two descriptions of defects in a benchmark for network-troubleshooting "
    "agents. A defect is a flaw in a task, a tool, or a trial's execution. Decide whether the "
    "audit record and the reference defect describe the same underlying defect: the same "
    "mechanism in the same component. Sharing a defect class or affected trials is not "
    'enough. Reply with JSON only: {"same": true or false, "reason": "<one sentence>"}.'
)


def load_truth(bench: str) -> dict[str, dict]:
    truth = json.loads((C.ROOT / "gt" / "gt.json").read_text())
    manifest = C.ROOT / "planted" / "manifest.yaml"
    if "planted" in bench and manifest.exists():
        excluded = C.ROOT / "planted" / "out_of_input.json"
        skip = (
            set(json.loads(excluded.read_text())["plants"])
            if excluded.exists()
            else set()
        )
        for p in yaml.safe_load(manifest.read_text())["plants"]:
            if p["id"] in skip:
                continue
            truth[p["id"]] = {
                "class": p["class"],
                "title": p["title"],
                "summary": p.get("mechanism", ""),
                "source": "planted",
                "trials": {
                    run: sorted(ts)
                    for run, ts in (p.get("mutated_trials") or {}).items()
                    if ts
                },
            }
    return truth


def detections(step: Path, arm: str) -> list[dict]:
    """Records or findings of one step with the trials they cover in this run."""
    if arm == "b3":
        try:
            found = json.loads((step / "findings.json").read_text())
        except (OSError, json.JSONDecodeError):
            return []
        return [
            {
                "id": f"F{i}",
                "class": f.get("class"),
                "title": f.get("title", ""),
                "summary": f.get("summary", ""),
                "evidence": str(f.get("excerpt", ""))[:600],
                "trials": set(f.get("trials") or []),
                "new": True,
            }
            for i, f in enumerate(found if isinstance(found, list) else [])
            if isinstance(f, dict)
        ]
    done = json.loads((step / "done.json").read_text())
    new = set(done.get("proposals") or [])
    before = previous_ids(step)
    final = {}
    for f in (step / "review" / "kb" / "issues").glob("*.yaml"):
        d = yaml.safe_load(f.read_text())
        refs = {r.split(":")[0] for r in (d.get("provenance") or {}).get("refs") or []}
        patterns = [
            str(s.get("pattern", "")) for s in d.get("detect") or [] if s.get("pattern")
        ]
        final[d["id"]] = {
            "id": d["id"],
            "class": d["class"],
            "title": d["title"],
            "summary": d.get("summary", ""),
            "evidence": ("signature: " + " | ".join(patterns))[:600]
            if patterns
            else "",
            "trials": refs,
            "new": f.name in new or d["id"] not in before,
        }
    audit = step / "audit-final.json"
    if audit.exists():
        report = json.loads(audit.read_text())
        for hit in report["known_issues"] + report["leads"]:
            if hit["id"] in final:
                final[hit["id"]]["trials"] |= set(hit["trials"])
    return list(final.values())


def previous_ids(step: Path) -> set[str]:
    done = json.loads((step / "done.json").read_text())
    if done["arm"] != "ours" or done["index"] == 0:
        return set()
    prev = sorted(step.parent.glob(f"{done['index'] - 1:02d}_*"))[0]
    return {
        yaml.safe_load(f.read_text())["id"]
        for f in (prev / "review" / "kb" / "issues").glob("*.yaml")
    }


def _pair_key(d: dict, gid: str, g: dict) -> str:
    text = [
        d["class"],
        d["title"],
        d["summary"],
        d.get("evidence", ""),
        gid,
        g["title"],
        g["summary"],
    ]
    return hashlib.sha256(json.dumps(text).encode()).hexdigest()


def judge(pairs: list[tuple[dict, str, dict]]) -> dict[str, bool]:
    """Same-defect decisions for (detection, gt id, gt) pairs, cached by content."""
    env = C.llm_env(JUDGE_MODEL)
    out: dict[str, bool] = {}
    todo = []
    for d, gid, g in pairs:
        k = _pair_key(d, gid, g)
        f = JUDGE_CACHE / k[:2] / f"{k}.json"
        if f.exists():
            out[k] = json.loads(f.read_text())["same"]
        elif k not in out:
            out[k] = None
            todo.append((k, d, g))

    def call(item):
        k, d, g = item
        user = (
            f"Audit record ({d['class']}): {d['title']}\n{d['summary']}\n{d.get('evidence', '')}\n\n"
            f"Reference defect ({g['class']}): {g['title']}\n{g['summary']}"
        )
        body = {
            "model": env["NIKA_AUDIT_LLM_MODEL"],
            "messages": [
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": user},
            ],
            "reasoning_effort": "low",
            "max_completion_tokens": 2000,
            "response_format": {"type": "json_object"},
        }
        for attempt in range(6):
            try:
                r = httpx.post(
                    env["NIKA_AUDIT_LLM_URL"] + "/chat/completions",
                    headers={"Authorization": f"Bearer {env['NIKA_AUDIT_LLM_KEY']}"},
                    json=body,
                    timeout=120,
                )
                if r.status_code == 429 or r.status_code >= 500:
                    raise httpx.HTTPStatusError("retry", request=r.request, response=r)
                r.raise_for_status()
                verdict = json.loads(r.json()["choices"][0]["message"]["content"])
                f = JUDGE_CACHE / k[:2] / f"{k}.json"
                f.parent.mkdir(parents=True, exist_ok=True)
                tmp = f.with_suffix(f".{os.getpid()}.tmp")
                tmp.write_text(
                    json.dumps(
                        {
                            "same": bool(verdict.get("same")),
                            "reason": verdict.get("reason", ""),
                            "record": d["title"],
                            "reference": g["title"],
                        }
                    )
                )
                tmp.replace(f)
                return k, bool(verdict.get("same"))
            except (httpx.HTTPError, json.JSONDecodeError, KeyError):
                import time

                time.sleep(2**attempt)
        return k, None

    with ThreadPoolExecutor(16) as ex:
        for k, v in ex.map(call, todo):
            out[k] = v
    return out


def candidates(d: dict, run: str, truth: dict[str, dict]) -> list[tuple[str, int]]:
    """Same-class defects overlapping the detection in this run, with overlap size."""
    out = []
    for gid, g in truth.items():
        if g["class"] != d["class"]:
            continue
        if g["trials"] is None:
            out.append((gid, 0))
        elif (ov := len(d["trials"] & set(g["trials"].get(run, [])))) > 0:
            out.append((gid, ov))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", default="nika-0.2.0-planted-t01")
    ap.add_argument("--arm", required=True)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    truth = load_truth(a.bench)
    steps = sorted((C.RUNS / a.bench / a.arm / f"s{a.seed}").glob("[0-9][0-9]_*"))
    plan = []
    for step in steps:
        if not (step / "done.json").exists():
            break
        run = step.name[3:]
        plan.append(
            (
                step,
                run,
                [(d, candidates(d, run, truth)) for d in detections(step, a.arm)],
            )
        )
    decisions = judge(
        [(d, gid, truth[gid]) for _, _, ds in plan for d, cs in ds for gid, _ in cs]
    )

    cumulative: set[str] = set()
    out, unmatched = [], []
    for step, run, ds in plan:
        found: set[str] = set()
        new_total = new_matched = undecided = 0
        for d, cs in ds:
            same = [
                (ov, gid)
                for gid, ov in cs
                if decisions.get(_pair_key(d, gid, truth[gid]))
            ]
            undecided += sum(
                decisions.get(_pair_key(d, gid, truth[gid])) is None for gid, _ in cs
            )
            credit = max(same)[1] if same else None
            if credit:
                found.add(credit)
            if d["new"]:
                new_total += 1
                new_matched += credit is not None
                if credit is None:
                    unmatched.append(
                        {
                            "key": f"{a.arm}/s{a.seed}/{step.name}/{d['id']}",
                            **{k: d[k] for k in ("class", "title", "summary")},
                            "trials": sorted(d["trials"])[:10],
                        }
                    )
        cumulative |= found
        out.append(
            {
                "step": step.name,
                "run": run,
                "detected": sorted(found),
                "cumulative": len(cumulative),
                "new_records": new_total,
                "new_matched": new_matched,
                "undecided": undecided,
            }
        )
    res = {
        "bench": a.bench,
        "arm": a.arm,
        "seed": a.seed,
        "truth": len(truth),
        "steps": out,
        "detected": sorted(cumulative),
    }
    C.write_json(C.RESULTS / a.bench / f"{a.arm}_s{a.seed}.json", res)
    with open(C.RESULTS / a.bench / f"unmatched_{a.arm}_s{a.seed}.jsonl", "w") as f:
        for u in unmatched:
            f.write(json.dumps(u) + "\n")
    print(
        json.dumps(
            {k: res[k] for k in ("arm", "seed", "truth")}
            | {"cumulative": [s["cumulative"] for s in out]}
        )
    )


if __name__ == "__main__":
    main()
