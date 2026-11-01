"""Turn adjudicated scan groups into issue files, without an LLM.

For each group the adjudicator called real, the curator decides whether the
memory already records it (a learned issue fires on the group's cited steps)
and, if not, writes one issue file. The signature is the longest text the
group's excerpts share, with numbers generalised; it must fire on the group's
own sources and mostly on the group's trials across the submission, so a
pattern that matches everywhere is not recorded. Title, summary, scope, and
provenance come from the group. A maintainer reviews the files before they
are merged.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import yaml

from auditlib.detectors import run_trial
from auditlib.kb import Issue, KnowledgeBase
from auditlib.review import check_issue
from auditlib.trace import Trial

PREFIX = {
    "shortcut": "LEAK",
    "harness_privilege": "POLICY",
    "out_of_scope": "OOS",
    "fault_not_realized": "FNR",
    "signal_invisible": "SIG",
    "label_unidentifiable": "LABEL",
    "healthy_lure": "LURE",
    "grading_error": "GRADE",
    "harness_defect": "HARNESS",
    "agent_integrity": "AGENT",
    "run_validity": "RUN",
}
_MIN_PATTERN = 12
_STOP = frozenset(
    "the a an and or of to in on for is are was were be by with from that this "
    "it its as at which when than then there their agent tool trial so not".split()
)


def _ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def common_text(excerpts: list[str]) -> str:
    """Longest substring all excerpts share, or the longest pairwise one."""
    texts = [_ws(e) for e in excerpts if _ws(e)]
    if not texts:
        return ""
    common = texts[0]
    for other in texts[1:]:
        m = SequenceMatcher(None, common, other, autojunk=False).find_longest_match(
            0, len(common), 0, len(other)
        )
        candidate = common[m.a : m.a + m.size]
        if len(candidate) < _MIN_PATTERN:
            break
        common = candidate
    return common.strip()


def to_pattern(text: str) -> str:
    """Escape *text* as a regex that also matches its JSON-escaped form.

    Tool results are often stored as JSON strings, so a quote may appear as
    ``\\"`` and a line break as ``\\n``. Digits and whitespace runs generalise.
    """
    out = []
    for token in re.split(r"(\d+|\s+|\")", text):
        if not token:
            continue
        if token.isdigit():
            out.append(r"\d+")
        elif token.isspace():
            out.append(r"(?:\s|\\[ntr])+")
        elif token == '"':
            out.append(r'\\?"')
        else:
            out.append(re.escape(token))
    return "".join(out)


def _slug(claim: str, used: set[str], prefix: str) -> str:
    words = [
        w.upper()
        for w in re.findall(r"[A-Za-z][A-Za-z0-9]+", claim)
        if w.lower() not in _STOP
    ][:4]
    base = f"{prefix}-" + "-".join(words or ["FINDING"])
    issue_id, n = base, 2
    while issue_id in used:
        issue_id, n = f"{base}-{n}", n + 1
    return issue_id


def _parse_ref(ref: Any) -> tuple[str, int] | None:
    trial, step = ref if isinstance(ref, (list, tuple)) else str(ref).rsplit(":", 1)
    try:
        return str(trial), int(step)
    except (TypeError, ValueError):
        return None


def _shares_text(a: str, b: str) -> bool:
    a, b = _ws(a).lower(), _ws(b).lower()
    m = SequenceMatcher(None, a, b, autojunk=False).find_longest_match(
        0, len(a), 0, len(b)
    )
    return m.size >= _MIN_PATTERN


def recorded_by(
    group: dict[str, Any],
    hits: dict[str, list[dict[str, Any]]],
    learned: dict[str, str],
    exact: frozenset[str] = frozenset(),
) -> str | None:
    """An issue of the group's class that already matches its evidence.

    *learned* maps the id of each issue that may record a group to its class. The issue must fire on one of the
    group's cited steps, and the text it matched must overlap one of the
    group's excerpts: a step often shows several unrelated things, so firing
    on the same step alone does not mean the same finding. Issues in *exact*
    identify the instance itself (a state-changing command at that step), so
    firing on the step is enough.
    """
    refs = {r for r in map(_parse_ref, group.get("all_refs") or []) if r}
    excerpts = (group.get("excerpts") or [])[:5]
    for trial, step in refs:
        for hit in hits.get(trial, []):
            issue = hit.get("issue")
            if (
                learned.get(issue) == group.get("class")
                and hit.get("step") == step
                and (
                    issue in exact
                    or any(_shares_text(hit.get("excerpt", ""), e) for e in excerpts)
                )
            ):
                return str(issue)
    return None


def covered_by_signature(group: dict[str, Any], issues: list[Issue]) -> str | None:
    """A learned issue whose text signature matches one of the group's excerpts.

    The same text signature means the same recorded finding, whatever class
    the scan filed the group under.
    """
    excerpts = [e for e in (group.get("excerpts") or [])[:10] if e.strip()]
    for issue in issues:
        for sig in issue.detect:
            if sig.kind == "text" and sig.regex is not None:
                if any(sig.regex.search(e) for e in excerpts):
                    return issue.id
    return None


def curate_group(
    group: dict[str, Any],
    trials: dict[str, Trial],
    kb: KnowledgeBase,
    kb_dir: Path,
    *,
    source: str,
    date: str,
    min_share: float = 0.5,
) -> dict[str, Any]:
    """Write one issue file for *group*, or say why not."""
    refs = [r for r in map(_parse_ref, group.get("all_refs") or []) if r]
    refs = [(t, s) for t, s in refs if t in trials and 0 <= s < len(trials[t].steps)]
    if not refs:
        return {"written": None, "why": "no cited step in the trials"}
    text = common_text(group.get("excerpts") or [])
    if len(text) < _MIN_PATTERN:
        return {"written": None, "why": "excerpts share no text to match"}
    kinds = sorted({trials[t].steps[s].kind for t, s in refs})
    used = {i.id for i in kb.issues} | {p.stem for p in (kb_dir / "issues").glob("*")}
    prefix = PREFIX.get(group["class"], "ISSUE")
    issue_id = _slug(group["claim"], used, prefix)
    envs = group.get("environments") or []
    all_envs = {t.scenario for t in trials.values()}
    data: dict[str, Any] = {
        "id": issue_id,
        "title": _ws(group["claim"])[:120],
        "class": group["class"],
        "severity": "warn",
        "status": "open",
        "summary": " ".join(
            dict.fromkeys(
                _ws(c) for c in [group["claim"], *group.get("other_claims", [])]
            )
        ),
        "guideline": (
            "Recorded from a scan group. Check that the matched text means the "
            "same thing in the new trial before counting it."
        ),
        "detect": [{"kind": "text", "where": kinds, "pattern": to_pattern(text)}],
        "provenance": {
            "source": source,
            "date": date,
            "refs": [f"{t}:{s}" for t, s in refs[:5]],
            "found_by": "agentic",
        },
    }
    if envs and len(envs) < len(all_envs) and len(envs) <= 3:
        data["scope"] = {"scenarios": sorted(envs)}
    sources = sorted({trials[t].path for t, _ in refs})
    issue = None
    # A match the agent typed itself is skipped by default; evidence the scan
    # cited can sit in such a step, so try without that rule before giving up.
    for authored in (True, False):
        data["detect"][0]["exclude_authored"] = authored
        try:
            candidate = Issue(**data)
        except (TypeError, ValueError) as exc:
            return {"written": None, "why": f"invalid issue: {exc}"}
        if check_issue(candidate, sources=sources, kb=kb)["ok"]:
            issue = candidate
            break
    if issue is None:
        return {"written": None, "why": "signature does not fire on its sources"}
    if data["detect"][0]["exclude_authored"]:
        del data["detect"][0]["exclude_authored"]
    single = kb.model_copy(update={"issues": [issue]})
    fired = {tid for tid, trial in trials.items() if run_trial(single, trial)}
    mine = {t for t, _ in refs}
    share = len(fired & mine) / len(fired) if fired else 0.0
    if share < min_share:
        return {
            "written": None,
            "why": f"signature too generic: {len(fired)} trials, {share:.0%} the group's",
        }
    path = kb_dir / "issues" / f"{issue_id}.yaml"
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )
    return {"written": issue_id, "fires_on": len(fired), "group_share": round(share, 2)}
