"""Group scan findings across trials and attach the evidence a curator needs.

A scan reports per trial, so one defect shows up as many findings: the same
excerpt in many trials, or the same observation in different words. This
module groups supported findings by class and normalised excerpt, merges
groups of one class whose claims share most of their words, and records for
each group where it occurs: fault and healthy trials, cases, environments, and
how many scan samples saw it. The curator reads groups, most widespread and
most agreed first, instead of thousands of findings.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from auditlib.trace import Trial

_MASKS = [
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?\b"), "<ip>"),
    (re.compile(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", re.I), "<mac>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"\d+"), "<n>"),
    (re.compile(r"\s+"), " "),
]
_WORD = re.compile(r"[a-z][a-z_./-]{2,}")
_STOP = frozenset(
    "the and that this with from for was were are has have not but its into "
    "which when than then they their there agent trial tool output".split()
)


def normalise(text: str) -> str:
    text = (text or "").lower()
    for pattern, repl in _MASKS:
        text = pattern.sub(repl, text)
    return text.strip()[:100]


def _words(text: str) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall((text or "").lower()) if w not in _STOP)


@dataclass
class Group:
    issue_class: str
    claims: list[str] = field(default_factory=list)
    refs: list[tuple[str, int]] = field(default_factory=list)
    excerpts: list[str] = field(default_factory=list)
    samples: set[tuple[str, int]] = field(default_factory=set)
    words: frozenset[str] = frozenset()

    def summary(self, trials: dict[str, Trial], n_samples: int) -> dict[str, Any]:
        hit = {t for t, _ in self.refs}
        known = [trials[t] for t in hit if t in trials]
        per_trial = defaultdict(set)
        for t, s in self.samples:
            per_trial[t].add(s)
        agree = (
            sum(len(s) for s in per_trial.values()) / (len(per_trial) * n_samples)
            if per_trial and n_samples
            else 0.0
        )
        return {
            "class": self.issue_class,
            "claim": self.claims[0],
            "other_claims": list(dict.fromkeys(self.claims[1:]))[:3],
            "trials": len(hit),
            "fault_trials": sum(not t.is_healthy_case for t in known),
            "healthy_trials": sum(t.is_healthy_case for t in known),
            "cases": len({t.case_key for t in known}),
            "environments": sorted({t.scenario for t in known}),
            "sample_agreement": round(agree, 2),
            "refs": [f"{t}:{s}" for t, s in sorted(set(self.refs))[:5]],
            "all_refs": sorted(set(self.refs)),
            "excerpts": list(dict.fromkeys(self.excerpts))[:20],
        }


def group_findings(
    records: list[dict[str, Any]],
    trials: dict[str, Trial],
    *,
    min_overlap: float = 0.5,
) -> list[dict[str, Any]]:
    """Group the supported findings of scan *records* (``audit.py scan`` output).

    Returns group summaries, most widespread and most agreed first.
    """
    n_samples = max((r.get("sample", 0) for r in records), default=0) + 1
    by_key: dict[tuple[str, str], Group] = {}
    for rec in records:
        for f in rec.get("findings", []):
            if f.get("citation_status") != "supported":
                continue
            cls = str(f.get("class") or "")
            key = (cls, normalise(f.get("excerpt") or ""))
            group = by_key.setdefault(key, Group(cls))
            group.claims.append(str(f.get("claim") or ""))
            group.refs.append((rec["trial"], f.get("step")))
            group.excerpts.append(str(f.get("excerpt") or ""))
            group.samples.add((rec["trial"], rec.get("sample", 0)))
    groups = list(by_key.values())
    for g in groups:
        g.words = _words(" ".join(g.claims[:5]))
    # Merge groups of one class whose claims mostly share words.
    merged: list[Group] = []
    for g in sorted(groups, key=lambda g: -len(g.refs)):
        for m in merged:
            if m.issue_class != g.issue_class or not g.words or not m.words:
                continue
            overlap = len(g.words & m.words) / min(len(g.words), len(m.words))
            if overlap >= min_overlap:
                m.claims += g.claims
                m.refs += g.refs
                m.excerpts += g.excerpts
                m.samples |= g.samples
                break
        else:
            merged.append(g)
    out = [g.summary(trials, n_samples) for g in merged]
    out.sort(key=lambda s: (-s["trials"], -s["sample_agreement"]))
    return out
