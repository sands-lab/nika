"""Build the planted-defect dataset for the audit-evolution experiment.

Each plant simulates one benchmark defect (a task, tool, or run bug) by
rewriting recorded trajectory files offline. A benchmark defect affects every
agent, so each plant is applied to the same task(s) in every evaluation run,
wherever the mutated artifact exists in a trial.

The dataset is a hard-link copy of the original runs; a mutated file is
written as a new file (the hard link is removed first), so the originals in
the trajectory store never change. The script is deterministic and rebuilds
the dataset from scratch:

    python plant.py            # build dataset, manifest and summary
    python plant.py --verify   # also load every trial and run the audit CLI

The manifest and summary are written outside the dataset directory.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(os.environ.get("AUDIT_EVO_ROOT", "/workspace/nika-audit-evolution"))
SRC = ROOT / "data" / "nika-0.2.0"
DST = ROOT / "data" / "nika-0.2.0-planted"
OUT = ROOT / "planted"
PYTHON = ROOT / "sandbox" / "tree" / ".venv" / "bin" / "python"

# The audit library and scorer come from the sandbox tree (0.2.0 code + audit).
sys.path.insert(0, str(ROOT / "sandbox" / "tree" / "src"))
from nika.audit.trace import load_trial, parse_messages, trial_dirs  # noqa: E402
from nika.evaluator.scoring import score_detection, score_rca_v2  # noqa: E402

METRIC_KEYS = (
    "rca_accuracy",
    "rca_precision",
    "rca_recall",
    "rca_f1",
    "localization_accuracy",
    "localization_precision",
    "localization_recall",
    "localization_f1",
    "fault_type_precision",
    "fault_type_recall",
    "fault_type_f1",
    "detection_score",
)

OVERLIMIT_WARNING = (
    "<warning>\n"
    "The output of your last command was too long.\n"
    "Please try a different command that produces less output.\n"
    "If you're looking at a file you can try use head, tail or sed to view a "
    "smaller number of lines selectively.\n"
    "If you're using grep or find and it produced too much output, you can use "
    "a more selective search pattern.\n"
    "If you really need to see something from the full command's output, you "
    "can redirect output to a file and then search in that file.\n"
    "</warning>\n"
)


# --------------------------------------------------------------------------
# Serialization helpers


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dump_json_file(obj: Any) -> str:
    """Format of every per-trial JSON file in the trajectory packages."""
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def dump_line(obj: Any, ensure_ascii: bool) -> str:
    return json.dumps(obj, ensure_ascii=ensure_ascii)


def line_style(line: str) -> bool:
    """ensure_ascii setting that reproduces an original JSONL line."""
    obj = json.loads(line)
    if json.dumps(obj) == line:
        return True
    if json.dumps(obj, ensure_ascii=False) == line:
        return False
    raise ValueError("JSONL line does not round-trip")


def py_literal(s: str, quote: str) -> str:
    """CPython ``repr`` of *s* with a forced quote character."""
    out = [quote]
    for ch in s:
        if ch == quote or ch == "\\":
            out.append("\\" + ch)
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif not ch.isprintable():
            o = ord(ch)
            if o < 0x100:
                out.append(f"\\x{o:02x}")
            elif o < 0x10000:
                out.append(f"\\u{o:04x}")
            else:
                out.append(f"\\U{o:08x}")
        else:
            out.append(ch)
    out.append(quote)
    return "".join(out)


def walk_replace(obj: Any, fn: Callable[[str], str]) -> tuple[Any, int]:
    """Apply *fn* to every string leaf; return the new object and change count."""
    if isinstance(obj, str):
        new = fn(obj)
        return new, int(new != obj)
    if isinstance(obj, list):
        n = 0
        out = []
        for v in obj:
            nv, k = walk_replace(v, fn)
            out.append(nv)
            n += k
        return out, n
    if isinstance(obj, dict):
        n = 0
        out = {}
        for k, v in obj.items():
            nv, c = walk_replace(v, fn)
            out[k] = nv
            n += c
        return out, n
    return obj, 0


# --------------------------------------------------------------------------
# Trial context


@dataclass
class TrialCtx:
    run: str
    trial_id: str
    src: Path  # resolved original trial directory
    dst: Path  # planted trial directory (hard links until written)
    pending: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def task(self) -> str:
        return self.trial_id.rsplit("__", 1)[0]

    def text(self, name: str) -> str | None:
        if name in self.pending:
            return self.pending[name]
        p = self.src / name
        return p.read_text(encoding="utf-8") if p.exists() else None

    def json(self, name: str) -> dict[str, Any] | None:
        t = self.text(name)
        return json.loads(t) if t is not None else None

    def put(self, name: str, text: str) -> None:
        orig = (
            (self.src / name).read_text(encoding="utf-8")
            if (self.src / name).exists()
            else None
        )
        if text == orig:
            self.pending.pop(name, None)
        else:
            self.pending[name] = text

    def put_json(self, name: str, obj: Any) -> None:
        self.put(name, dump_json_file(obj))


# --------------------------------------------------------------------------
# messages.jsonl / nika.jsonl rewriting


class Jsonl:
    """A JSONL file whose rows can be edited; untouched lines stay byte-identical."""

    def __init__(self, text: str):
        self.trailing = text.endswith("\n")
        self.lines = text.split("\n")
        if self.trailing:
            self.lines = self.lines[:-1]
        self._rows: dict[int, Any] = {}
        self.dirty: set[int] = set()

    def row(self, i: int) -> Any:
        if i not in self._rows:
            self._rows[i] = json.loads(self.lines[i])
        return self._rows[i]

    def try_row(self, i: int) -> Any:
        """Parsed row, or None for blank or unreadable lines (kept verbatim)."""
        if not self.lines[i]:
            return None
        try:
            return self.row(i)
        except json.JSONDecodeError:
            return None

    def set_row(self, i: int, obj: Any) -> None:
        self._rows[i] = obj
        self.dirty.add(i)

    def text(self) -> str:
        out = []
        for i, line in enumerate(self.lines):
            if i in self.dirty:
                out.append(dump_line(self._rows[i], line_style(line) if line else True))
            else:
                out.append(line)
        return "\n".join(out) + ("\n" if self.trailing else "")

    def __len__(self) -> int:
        return len(self.lines)


def unwrap(text: str) -> tuple[str, Callable[[str], str] | None]:
    """Split a recorded tool result into the tool's own output and a re-wrapper.

    Claude Code records MCP results as ``{"result": ...}``; other harnesses
    record the output itself.
    """
    if text.startswith('{"result":'):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return text, None
        if isinstance(obj, dict) and list(obj) == ["result"]:
            for ea in (True, False):
                if json.dumps(obj, separators=(",", ":"), ensure_ascii=ea) != text:
                    continue
                value = obj["result"]
                if isinstance(value, str):
                    return value, lambda new, ea=ea: json.dumps(
                        {"result": new}, separators=(",", ":"), ensure_ascii=ea
                    )
                inner = json.dumps(value, separators=(",", ":"), ensure_ascii=ea)
                return inner, lambda new, ea=ea: json.dumps(
                    {"result": json.loads(new)}, separators=(",", ":"), ensure_ascii=ea
                )
            return text, None
    return text, lambda new: new


def later_agent_text(ctx: TrialCtx, steps, index: int) -> str:
    """Everything the agent wrote after step *index* (texts, reasoning, tool inputs)."""
    parts = [
        s.text
        for s in steps
        if s.index > index and s.kind in ("agent_text", "reasoning", "tool_call")
    ]
    sub = ctx.json("submission.json") or {}
    parts.append(str(sub.get("diagnosis_report") or ""))
    parts.append(json.dumps(sub.get("root_causes") or []))
    return "\n".join(parts)


def call_input(step) -> dict[str, Any]:
    try:
        v = json.loads(step.text)
        return v if isinstance(v, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


# Per-plant counters of rewritten and guard-skipped tool results.
REWRITE_STATS: dict[str, dict[str, int]] = defaultdict(
    lambda: {"candidates": 0, "changed": 0, "guarded": 0}
)
CURRENT_PLANT = {"id": ""}

OutputFn = Callable[[Any, dict[str, Any], str], "str | None"]
GuardFn = Callable[[str, str], list[str]]


def rewrite_outputs(
    ctx: TrialCtx,
    fn: OutputFn,
    guard: GuardFn | None = None,
    tools: tuple[str, ...] | None = None,
) -> dict[str, int]:
    """Rewrite tool outputs in messages.jsonl.

    *fn(call_step, call_input, output)* returns the new tool output or None.
    *guard(old, new)* returns tokens that the mutation removes; when the agent
    quotes any of them later in the trial, the call is left unchanged so the
    trace never contradicts what the agent wrote.
    """
    stats = {"changed": 0, "guarded": 0}
    path = ctx.src / "messages.jsonl"
    steps, _ = parse_messages(path)
    calls = {s.call_id: s for s in steps if s.kind == "tool_call" and s.call_id}
    edits: list[tuple[int, str, str, str, str]] = []
    for res in steps:
        if res.kind != "tool_result":
            continue
        call = calls.get(res.call_id)
        if call is None or (tools and call.tool not in tools):
            continue
        inner, rewrap = unwrap(res.text)
        if rewrap is None:
            continue
        new_inner = fn(call, call_input(call), inner)
        if new_inner is None or new_inner == inner:
            continue
        REWRITE_STATS[CURRENT_PLANT["id"]]["candidates"] += 1
        if guard is not None:
            toks = [t for t in guard(inner, new_inner) if t]
            later = later_agent_text(ctx, steps, res.index)
            if any(t in later for t in toks):
                stats["guarded"] += 1
                REWRITE_STATS[CURRENT_PLANT["id"]]["guarded"] += 1
                continue
        edits.append((res.line - 1, res.text, rewrap(new_inner), inner, new_inner))
    if not edits:
        return stats
    doc = Jsonl(ctx.text("messages.jsonl"))
    # Exact replacement of the recorded result on its own row.
    for line, old_full, new_full, _, _ in edits:
        row = doc.row(line)
        new_row, n = walk_replace(
            row, lambda s, o=old_full, nw=new_full: nw if s == o else s
        )
        if n == 0:
            raise RuntimeError(
                f"{ctx.trial_id}: result text not found on line {line + 1}"
            )
        doc.set_row(line, new_row)
    # LangGraph rows repeat earlier tool messages as Python reprs.
    pairs = []
    for _, _, _, old, new in edits:
        if len(old) >= 30:
            for q in ("'", '"'):
                pairs.append((py_literal(old, q), py_literal(new, q)))
    if pairs:
        for i in range(len(doc)):
            if not doc.lines[i] or "llm_start" not in doc.lines[i][:200]:
                continue
            row = doc.try_row(i)
            if not isinstance(row, dict) or row.get("event") != "llm_start":
                continue

            def fix(s: str) -> str:
                for a, b in pairs:
                    if a in s:
                        s = s.replace(a, b)
                return s

            new_row, n = walk_replace(row, fix)
            if n:
                doc.set_row(i, new_row)
    ctx.put("messages.jsonl", doc.text())
    stats["changed"] = len(edits)
    REWRITE_STATS[CURRENT_PLANT["id"]]["changed"] += len(edits)
    return stats


def replace_everywhere(
    ctx: TrialCtx,
    name: str,
    old: str,
    new: str,
    *,
    events: tuple[str, ...] | None = None,
) -> int:
    """Replace a substring in every string leaf of a JSONL file (also inside reprs)."""
    text = ctx.text(name)
    if text is None:
        return 0
    doc = Jsonl(text)
    raw = [(old, new)]
    # LangGraph llm_start rows hold the conversation as Python reprs.
    reprs = []
    for q in ("'", '"'):
        pair = (py_literal(old, q)[1:-1], py_literal(new, q)[1:-1])
        if pair not in reprs:
            reprs.append(pair)
    total = 0
    for i in range(len(doc)):
        row = doc.try_row(i)
        if not isinstance(row, dict):
            continue
        if events and row.get("event") not in events:
            continue
        variants = reprs if row.get("event") == "llm_start" else raw

        def fix(s: str, variants=variants) -> str:
            for a, b in variants:
                if a in s:
                    return s.replace(a, b)
            return s

        new_row, n = walk_replace(row, fix)
        if n:
            doc.set_row(i, new_row)
            total += n
    if total:
        ctx.put(name, doc.text())
    return total


# --------------------------------------------------------------------------
# Scoring helpers


def score(sub: dict[str, Any], gt: dict[str, Any]) -> dict[str, float]:
    s = dict(score_rca_v2(sub, gt))
    s["detection_score"] = score_detection(sub, gt)
    return s


def set_metrics(ctx: TrialCtx, buggy: Callable[[dict, dict], dict[str, float]]) -> bool:
    """Replace recorded scores with *buggy(sub, gt)* where they match a correct grade.

    Recorded values that already differ from the current scorer (older
    scoring revisions) are kept, so real grading differences stay intact.
    """
    sub = ctx.json("submission.json")
    gt = ctx.json("ground_truth.json")
    em = ctx.json("eval_metrics.json")
    if not sub or not gt or not em:
        return False
    try:
        right = score(sub, gt)
        wrong = buggy(sub, gt)
    except Exception as exc:  # noqa: BLE001
        ctx.notes.append(f"scoring skipped: {exc}")
        return False
    new = dict(em)
    for k in METRIC_KEYS:
        if k in em and isinstance(em[k], (int, float)) and abs(em[k] - right[k]) < 1e-6:
            new[k] = wrong[k]
    if new == em:
        return False
    ctx.put_json("eval_metrics.json", new)
    run = ctx.json("run.json")
    if isinstance(run.get("eval_metrics"), dict):
        rem = dict(run["eval_metrics"])
        for k in METRIC_KEYS:
            if k in rem and k in new:
                rem[k] = new[k]
        run["eval_metrics"] = rem
        ctx.put_json("run.json", run)
    return True


def rescore_with_gt(ctx: TrialCtx, gt_new: dict[str, Any]) -> None:
    """Write a mutated ground truth and scores consistent with it."""
    ctx.put_json("ground_truth.json", gt_new)
    sub = ctx.json("submission.json")
    em = ctx.json("eval_metrics.json")
    if not sub or not em:
        return
    orig_gt = json.loads((ctx.src / "ground_truth.json").read_text())
    right = score(sub, orig_gt)
    new_scores = score(sub, gt_new)
    new = dict(em)
    for k in METRIC_KEYS:
        if k in em and isinstance(em[k], (int, float)) and abs(em[k] - right[k]) < 1e-6:
            new[k] = new_scores[k]
    if new != em:
        ctx.put_json("eval_metrics.json", new)
        run = ctx.json("run.json")
        if isinstance(run.get("eval_metrics"), dict):
            rem = dict(run["eval_metrics"])
            for k in METRIC_KEYS:
                if k in rem:
                    rem[k] = new[k]
            run["eval_metrics"] = rem
            ctx.put_json("run.json", run)


def rc(fault_type: str, resource: dict[str, Any]) -> dict[str, Any]:
    return {
        "fault_type": fault_type,
        "resource": resource,
        "resource_id": resource["id"],
    }


def node_res(node: str) -> dict[str, Any]:
    return {"id": f"node/{node}", "kind": "node", "node": node}


def intf_res(node: str, name: str) -> dict[str, Any]:
    return {
        "id": f"interface/{node}/{name}",
        "kind": "interface",
        "name": name,
        "node": node,
    }


# --------------------------------------------------------------------------
# Generic text helpers used by several plants


TOKEN_RX = re.compile(
    r"\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d+)?\b|\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b|\b\d{4,}\b|\b[0-9a-f]{12,}\b"
)


def removed_tokens(old: str, new: str) -> list[str]:
    """Distinctive tokens present in *old* but not in *new*."""
    keep = set(TOKEN_RX.findall(new))
    return sorted({t for t in TOKEN_RX.findall(old) if t not in keep})


def removed_lines(old: str, new: str, min_len: int = 18) -> list[str]:
    keep = set(new.splitlines())
    return [
        ln.strip()
        for ln in old.splitlines()
        if ln not in keep and len(ln.strip()) >= min_len and re.search(r"\d", ln)
    ]


def guard_removed(old: str, new: str) -> list[str]:
    return removed_tokens(old, new) + removed_lines(old, new)


def host_of(inp: dict[str, Any]) -> str:
    return str(
        inp.get("host_name") or inp.get("device_name") or inp.get("device") or ""
    )


def overlimit(text: str, head: int, tail: int) -> str:
    elided = len(text) - head - tail
    return (
        OVERLIMIT_WARNING
        + "<output_head>\n"
        + text[:head]
        + "\n</output_head>\n<elided_chars>\n"
        + f"{elided} characters elided\n"
        + "</elided_chars>\n<output_tail>\n"
        + text[-tail:]
        + "\n</output_tail>"
    )


# Whole-table forms of iproute2 commands; per-interface or per-prefix forms are left alone.
_END = r"\s*(?:$|[;&|]|2>|>|\))"
ROUTE_CMD = re.compile(
    r"\bip (?:-4 )?r(?:o|ou|out|oute)?(?: show| list| s| ls)?" + _END
)
ADDR_CMD = re.compile(
    r"\bip (?:-4 )?a(?:d|dd|ddr|ddress)?(?: show| list| s| ls)?" + _END
)
BR_ADDR_CMD = re.compile(r"\bip -br(?:ief)? a(?:d|dd|ddr|ddress)?(?: show| s)?" + _END)


def is_overlimit(text: str) -> bool:
    return text.lstrip().startswith(
        "<warning>\nThe output of your last command was too long"
    )


# --------------------------------------------------------------------------
# Plants


@dataclass
class Plant:
    id: str
    cls: str
    title: str
    mechanism: str
    tasks: list[str]
    controls: list[str]
    files: list[str]
    apply: Callable[[TrialCtx, "Plant", dict], None]
    params: dict[str, Any] = field(default_factory=dict)


# ---- grading_error ---------------------------------------------------------


def p_alias_label(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """Scorer maps the task's fault type through a stale alias table."""
    old, alias = plant.params["fault_type"], plant.params["graded_as"]

    def buggy(sub, gt):
        g = copy.deepcopy(gt)
        for r in g.get("root_causes") or []:
            if r.get("fault_type") == old:
                r["fault_type"] = alias
        return score(sub, g)

    set_metrics(ctx, buggy)


def p_any_match(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """Multi-root-cause task graded with any-match semantics."""

    def buggy(sub, gt):
        s = score(sub, gt)
        if s["rca_recall"] > 0:
            perfect = score({"is_anomaly": True, "root_causes": gt["root_causes"]}, gt)
            for k in ("rca_accuracy", "rca_precision", "rca_recall", "rca_f1"):
                s[k] = perfect[k]
        if s["localization_recall"] > 0:
            perfect = score({"is_anomaly": True, "root_causes": gt["root_causes"]}, gt)
            for k in (
                "localization_accuracy",
                "localization_precision",
                "localization_recall",
                "localization_f1",
            ):
                s[k] = perfect[k]
        return s

    set_metrics(ctx, buggy)


def p_shared_scorecard(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """Scores are written to a case-level path; the last trial's file wins."""
    last = shared.get("last_trial")
    if last is None or last == ctx.src:
        return
    em_text = (last / "eval_metrics.json").read_text()
    ctx.put("eval_metrics.json", em_text)
    run = ctx.json("run.json")
    if isinstance(run.get("eval_metrics"), dict):
        run["eval_metrics"] = json.loads(em_text)
        ctx.put_json("run.json", run)


def p_prefix_match(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """Fault types compared on their family prefix (first two words)."""

    def fam(t: str) -> str:
        return "_".join(str(t).split("_")[:2])

    def buggy(sub, gt):
        gt_types = {r["fault_type"] for r in gt.get("root_causes") or []}
        s2 = copy.deepcopy(sub)
        for r in s2.get("root_causes") or []:
            if isinstance(r, dict):
                for g in gt_types:
                    if r.get("fault_type") != g and fam(r.get("fault_type", "")) == fam(
                        g
                    ):
                        r["fault_type"] = g
        return score(s2, gt)

    set_metrics(ctx, buggy)


# ---- label_unidentifiable ----------------------------------------------------


def p_link_order(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """Ground-truth link id written with endpoints in topology order, not canonical order."""
    gt = ctx.json("ground_truth.json")
    g = copy.deepcopy(gt)
    for r in g.get("root_causes") or []:
        res = r.get("resource") or {}
        if res.get("kind") == "link":
            ends = res["name"].split("--")
            name = "--".join(reversed(ends))
            res["name"] = name
            res["id"] = "link/" + name
            r["resource_id"] = res["id"]
    if g != gt:
        rescore_with_gt(ctx, g)


def p_append_observer(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """Ground truth lists the probe observer device as a second root cause."""
    gt = ctx.json("ground_truth.json")
    g = copy.deepcopy(gt)
    ft = g["root_causes"][0]["fault_type"]
    g["root_causes"].append(rc(ft, node_res(plant.params["observer"])))
    rescore_with_gt(ctx, g)


def p_granularity(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """Node-level fault labelled with the injector's port parameter."""
    gt = ctx.json("ground_truth.json")
    g = copy.deepcopy(gt)
    for r in g["root_causes"]:
        if (
            r["resource"].get("kind") == "node"
            and r["resource"]["node"] == plant.params["node"]
        ):
            new = intf_res(plant.params["node"], plant.params["intf"])
            r["resource"] = new
            r["resource_id"] = new["id"]
    if g != gt:
        rescore_with_gt(ctx, g)


def p_ontology_dup(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """Submission context renders the sibling label's description for the true label."""
    p = plant.params
    old = f'{{"description": "{p["desc"]}", "id": "{p["label"]}"'
    new = f'{{"description": "{p["sibling_desc"]}", "id": "{p["label"]}"'
    n = replace_everywhere(ctx, "messages.jsonl", old, new)
    if n == 0:
        ctx.notes.append("ontology entry not found")


# ---- harness_defect ----------------------------------------------------------


def p_empty_host(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """exec_shell on one device returns an empty, successful result."""
    host = plant.params["host"]

    def fn(call, inp, out):
        if host_of(inp) != host or not out.strip() or is_overlimit(out):
            return None
        return ""

    shared.setdefault("stats", []).append(
        rewrite_outputs(ctx, fn, guard_removed, tools=("exec_shell",))
    )


def p_submit_drop(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """Submission normaliser drops interface ids whose name is not ethN; submit still succeeds."""
    sub = ctx.json("submission.json")
    if not sub:
        return
    rcs = sub.get("root_causes") or []
    rx = re.compile(r"^interface/[^/]+/(?!eth\d+$)[^/]+$")
    keep = [
        r
        for r in rcs
        if not (isinstance(r, dict) and rx.match(str(r.get("resource_id") or "")))
    ]
    if len(keep) == len(rcs):
        return
    s2 = dict(sub)
    s2["root_causes"] = keep
    ctx.put_json("submission.json", s2)
    em = ctx.json("eval_metrics.json")
    gt = ctx.json("ground_truth.json")
    if em and gt:
        right = score(sub, gt)
        wrong = score(s2, gt)
        new = dict(em)
        for k in METRIC_KEYS:
            if k in em and abs(em[k] - right[k]) < 1e-6:
                new[k] = wrong[k]
        ctx.put_json("eval_metrics.json", new)
        run = ctx.json("run.json")
        if isinstance(run.get("eval_metrics"), dict):
            rem = dict(run["eval_metrics"])
            for k in METRIC_KEYS:
                if k in rem:
                    rem[k] = new[k]
            run["eval_metrics"] = rem
            ctx.put_json("run.json", run)


def p_silent_truncation(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """CLI tool output cut at a byte budget with no marker."""
    cap = plant.params["cap"]
    tool = plant.params["tool"]

    def fn(call, inp, out):
        if is_overlimit(out):
            return None
        raw = out.encode("utf-8")
        if len(raw) <= cap:
            return None
        return raw[:cap].decode("utf-8", errors="ignore")

    rewrite_outputs(ctx, fn, guard_removed, tools=(tool,))


def p_events_empty(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """k8s event listing returns an empty list instead of the events."""

    def fn(call, inp, out):
        if is_overlimit(out) or out.strip() in ("", "[]"):
            return None
        try:
            v = json.loads(out)
        except json.JSONDecodeError:
            return None
        if not isinstance(v, list) or not v:
            return None
        return "[]"

    def guard(old, new):
        try:
            v = json.loads(old)
        except json.JSONDecodeError:
            return []
        toks = []
        for e in v if isinstance(v, list) else []:
            if isinstance(e, dict):
                toks.append(str(e.get("name") or ""))
                msg = str(e.get("message") or "")
                if len(msg) > 20:
                    toks.append(msg[:40])
        return toks

    rewrite_outputs(ctx, fn, guard, tools=("k8s_list_events",))


# ---- run_validity ------------------------------------------------------------


def p_max_tokens(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """Task-level agent override lowers the generation budget for this task only."""
    run = ctx.json("run.json")
    orig = run.get("max_tokens")
    if not isinstance(orig, int):
        return
    new = 4096 if orig > 4096 else max(1024, orig // 2)
    run["max_tokens"] = new
    ctx.put_json("run.json", run)
    # LangGraph records the model constructor, including extra_body.max_tokens.
    text = ctx.text("messages.jsonl")
    if '"extra_body"' in text:
        doc = Jsonl(text)
        for i in range(len(doc)):
            if not doc.lines[i] or '"extra_body"' not in doc.lines[i]:
                continue
            row = doc.try_row(i)
            if not isinstance(row, dict):
                continue
            kw = (row.get("model") or {}).get("kwargs") or {}
            eb = kw.get("extra_body")
            if isinstance(eb, dict) and isinstance(eb.get("max_tokens"), int):
                eb["max_tokens"] = (
                    new if eb["max_tokens"] == orig else min(eb["max_tokens"], new)
                )
                doc.set_row(i, row)
        ctx.put("messages.jsonl", doc.text())


def p_budget(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """Task-level override shortens the agent step and time budget."""
    text = ctx.text("nika.jsonl")
    doc = Jsonl(text)
    changed = False
    for i in range(len(doc)):
        if '"agent_start"' not in doc.lines[i]:
            continue
        row = doc.try_row(i)
        if not isinstance(row, dict):
            continue
        data = row.get("data") or {}
        if row.get("event") != "agent_start" or "max_steps" not in data:
            continue
        data["max_steps"] = plant.params["max_steps"]
        if isinstance(data.get("timeout_sec"), (int, float)):
            data["timeout_sec"] = plant.params["timeout_sec"]
        doc.set_row(i, row)
        changed = True
    if changed:
        ctx.put("nika.jsonl", doc.text())


def p_duplicate_trial(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """Result publisher resolved the third trial to the second trial's session."""
    if not ctx.trial_id.endswith("__t03"):
        return
    twin = ctx.src.parent / (ctx.task + "__t02")
    if not twin.is_dir():
        return
    for name in sorted(os.listdir(twin)):
        p = twin / name
        if p.is_file():
            ctx.put(name, p.read_text(encoding="utf-8"))


# ---- shortcut ------------------------------------------------------------------


def p_rule_comment(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """Injector tags its nftables rules with the fault name."""
    rule, tag = plant.params["rule"], plant.params["tag"]
    rx = re.compile(r"^([ \t]*" + re.escape(rule) + r")[ \t]*$", re.M)

    def fn(call, inp, out):
        if rule not in out:
            return None
        return rx.sub(lambda m: f'{m.group(1)} comment "{tag}"', out)

    rewrite_outputs(ctx, fn, tools=("exec_shell",))


def p_commented_original(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """Injector comments out the original option instead of replacing it."""
    rx = re.compile(r"^([ \t]*)option routers (\d+\.\d+\.\d+)\.(\d+);[ \t]*$", re.M)

    def fn(call, inp, out):
        if "option routers" not in out or "subnet " not in out:
            return None

        def sub(m):
            if m.group(3) == "1":
                return m.group(0)
            return f"{m.group(1)}#option routers {m.group(2)}.1;\n{m.group(0)}"

        new = rx.sub(sub, out)
        return new if new != out else None

    rewrite_outputs(ctx, fn, tools=("exec_shell",))


def p_prompt_hint(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """Task brief rendered with a note derived from the injection parameters."""
    run = ctx.json("run.json")
    desc = run.get("task_description") or ""
    m = re.search(r"^Topology: .*$", desc, re.M)
    if not m:
        ctx.notes.append("no topology line")
        return
    anchor = m.group(0)[-48:]
    if desc.count(anchor) != 1:
        ctx.notes.append("anchor not unique")
        return
    new = anchor + "\n" + plant.params["note"]
    run["task_description"] = desc.replace(anchor, new)
    ctx.put_json("run.json", run)
    n = replace_everywhere(
        ctx, "messages.jsonl", anchor, new, events=("prompt", "llm_start")
    )
    if n == 0:
        ctx.notes.append("prompt anchor not found in messages")


# ---- healthy_lure -------------------------------------------------------------


def p_idle_peer(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """A by-design BGP peer for an undeployed leaf sits in Active state."""
    p = plant.params
    hdr = "Neighbor        V         AS   MsgRcvd   MsgSent   TblVer  InQ OutQ  Up/Down State/PfxRcd   PfxSnt Desc"
    row = f"{p['peer']:<16}4 {p['asn']:>10} {0:>9} {0:>9} {0:>8} {0:>4} {0:>4} {'never':>8} {'Active':>12} {0:>8} N/A"

    def fn(call, inp, out):
        if host_of(inp) != p["host"] or hdr not in out or p["peer"] + " " in out:
            return None
        lines = out.split("\n")
        i = lines.index(hdr) if hdr in lines else -1
        if i < 0:
            return None
        j = i + 1
        while j < len(lines) and re.match(r"^\d+\.\d+\.\d+\.\d+\s", lines[j]):
            j += 1
        lines.insert(j, row)
        new = "\n".join(lines)
        new = re.sub(
            r"^Peers (\d+),",
            lambda m: f"Peers {int(m.group(1)) + 1},",
            new,
            count=1,
            flags=re.M,
        )
        new = re.sub(
            r"^Total number of neighbors (\d+)",
            lambda m: f"Total number of neighbors {int(m.group(1)) + 1}",
            new,
            count=1,
            flags=re.M,
        )
        return new

    rewrite_outputs(ctx, fn, tools=("exec_shell",))


def p_stale_device(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """Controller store keeps a decommissioned switch as an unavailable device."""
    dev = plant.params["device"]

    def fn(call, inp, out):
        path = str(inp.get("path") or "").strip().lstrip("/").split("?")[0].rstrip("/")
        if path != "onos/v1/devices" or is_overlimit(out):
            return None
        try:
            obj = json.loads(out)
        except json.JSONDecodeError:
            return None
        if json.dumps(obj, indent=2) != out:
            return None
        devs = (
            ((obj.get("body") or {}).get("devices")) if isinstance(obj, dict) else None
        )
        if (
            not isinstance(devs, list)
            or not devs
            or any(d.get("id") == dev["id"] for d in devs)
        ):
            return None
        tmpl = devs[0]
        d = copy.deepcopy(tmpl)
        d.update({k: v for k, v in dev.items() if k != "annotations"})
        if isinstance(d.get("annotations"), dict):
            d["annotations"] = dict(d["annotations"], **dev["annotations"])
        devs.append(d)
        return json.dumps(obj, indent=2)

    rewrite_outputs(ctx, fn, tools=("sdn_onos_rest",))


def p_aggregate_blackhole(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """BGP aggregate installs a discard route on a super-spine."""
    p = plant.params
    line = p["route"]

    def fn(call, inp, out):
        if host_of(inp) != p["host"] or line.split(" ")[1] in out:
            return None
        cmd = str(inp.get("command") or "")
        if not ROUTE_CMD.search(cmd):
            return None
        lines = out.split("\n")
        for i, ln in enumerate(lines):
            if re.match(r"^10\.\d+\.\d+\.\d+/\d+ (nhid \d+ )?(proto bgp|via )", ln):
                lines.insert(i, line)
                return "\n".join(lines)
        return None

    rewrite_outputs(ctx, fn, tools=("exec_shell",))


# ---- harness_privilege / out_of_scope ------------------------------------------


def p_extra_mcp(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """Scenario registry starts a lab administration MCP server for the agent."""
    server, tools = plant.params["server"], plant.params["tools"]
    text = ctx.text("messages.jsonl")
    doc = Jsonl(text)
    changed = False
    for i in range(len(doc)):
        ln = doc.lines[i]
        if not ln or ('"mcp_config"' not in ln and '"init"' not in ln):
            continue
        row = doc.try_row(i)
        if not isinstance(row, dict) or row.get("phase") != "diagnosis":
            continue
        if row.get("event") == "mcp_config" and isinstance(row.get("servers"), list):
            if server not in row["servers"]:
                row["servers"].append(server)
                doc.set_row(i, row)
                changed = True
        ce = row.get("claude_event")
        if isinstance(ce, dict) and ce.get("subtype") == "init":
            tl = ce.get("tools")
            if isinstance(tl, list):
                names = [f"mcp__{server}__{t}" for t in tools]
                # Claude Code lists MCP tools grouped by server after built-ins.
                idx = (
                    max(
                        (
                            k
                            for k, t in enumerate(tl)
                            if str(t).startswith("mcp__kathara")
                        ),
                        default=len(tl) - 1,
                    )
                    + 1
                )
                for n in names:
                    if n not in tl:
                        tl.insert(idx, n)
                        idx += 1
            ms = ce.get("mcp_servers")
            if isinstance(ms, list) and not any(
                isinstance(m, dict) and m.get("name") == server for m in ms
            ):
                pos = (
                    max(
                        (
                            k
                            for k, m in enumerate(ms)
                            if isinstance(m, dict) and m.get("source") == "dynamic"
                        ),
                        default=-1,
                    )
                    + 1
                )
                ms.insert(
                    pos, {"name": server, "status": "connected", "source": "dynamic"}
                )
            doc.set_row(i, row)
            changed = True
    if changed:
        ctx.put("messages.jsonl", doc.text())


def _ip_addr_block(idx: int, name: str, mac: str, addr: str, brd: str) -> str:
    return (
        f"{idx}: {name}: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue state UP group default qlen 1000\n"
        f"    link/ether {mac} brd ff:ff:ff:ff:ff:ff\n"
        f"    inet {addr} brd {brd} scope global {name}\n"
        f"       valid_lft forever preferred_lft forever"
    )


def p_bridged_mgmt(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """Lab device attached to the host's container bridge (Kathara bridged option)."""
    p = plant.params
    name, addr, net = p["intf"], p["addr"], p["net"]
    ip = addr.split("/")[0]

    def fn(call, inp, out):
        if host_of(inp) != p["host"] or f" {name}" in out or is_overlimit(out):
            return None
        cmd = str(inp.get("command") or "")
        lines = out.split("\n")
        changed = False
        # ip addr (full format)
        hdr = [i for i, ln in enumerate(lines) if re.match(r"^\d+: \w+(@\S+)?: <", ln)]
        if hdr and ADDR_CMD.search(cmd):
            last = hdr[-1]
            j = last + 1
            while j < len(lines) and lines[j].startswith(" "):
                j += 1
            idx = max(int(lines[i].split(":")[0]) for i in hdr) + 3
            block = _ip_addr_block(idx, name, p["mac"], addr, p["brd"])
            if "inet " not in "\n".join(lines[last:j]):
                return None
            lines[j:j] = block.split("\n")
            changed = True
        # ip -br addr
        br = [
            i
            for i, ln in enumerate(lines)
            if re.match(r"^eth\d+(@\S+)?\s+(UP|DOWN|UNKNOWN)\s+\d", ln)
        ]
        if br and BR_ADDR_CMD.search(cmd):
            lines.insert(br[-1] + 1, f"{name:<17}UP             {addr} ")
            changed = True
        # ip route
        kern = [
            i
            for i, ln in enumerate(lines)
            if re.match(r"^\S+ dev eth\d+ proto kernel scope link src ", ln)
        ]
        if kern and ROUTE_CMD.search(cmd):
            lines.insert(
                kern[-1] + 1, f"{net} dev {name} proto kernel scope link src {ip} "
            )
            changed = True
        return "\n".join(lines) if changed else None

    rewrite_outputs(ctx, fn, tools=("exec_shell",))


def p_guard_process(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """Harness FRR guard process and its state file visible inside lab devices."""
    proc = plant.params["command"]

    def grep_filters(cmd: str) -> list[tuple[bool, re.Pattern]] | None:
        seg = cmd.split("ps ", 1)
        if len(seg) < 2:
            return None
        after = seg[1].split(";")[0].split("&&")[0]
        parts = after.split("|")[1:]
        out = []
        for part in parts:
            part = part.strip()
            if not part.startswith("grep"):
                if part.startswith(("head", "sort", "awk", "wc", "cut")):
                    return None
                continue
            m = re.match(
                r"grep((?:\s+-[a-zA-Z]+)*)\s+(?:'([^']*)'|\"([^\"]*)\"|(\S+))", part
            )
            if not m:
                return None
            flags = m.group(1) or ""
            pat = m.group(2) or m.group(3) or m.group(4) or ""
            try:
                rx = re.compile(pat.replace("\\|", "|"), re.I if "i" in flags else 0)
            except re.error:
                return None
            out.append(("v" in flags, rx))
        return out

    row_rx = re.compile(
        r"^(\S+)\s+(\d+)\s+[\d.]+\s+[\d.]+\s+\d+\s+\d+\s+\S+\s+\S+\s+(\S+)\s+\S+\s+.+$"
    )

    def fn(call, inp, out):
        cmd = str(inp.get("command") or "")
        if (
            not re.search(r"\bps\s+(aux|-ef|-aux|axu|auxww)\b", cmd)
            or "frr_guard" in out
        ):
            return None
        filters = grep_filters(cmd)
        if filters is None:
            return None
        lines = out.split("\n")
        rows = [(i, row_rx.match(ln)) for i, ln in enumerate(lines)]
        rows = [(i, m) for i, m in rows if m]
        if not rows:
            return None
        pid = plant.params["pid"]
        if any(int(m.group(2)) == pid for _, m in rows):
            return None
        first = min(rows, key=lambda r: int(r[1].group(2)))
        start = first[1].group(3)
        new = f"root     {pid:>7}  0.0  0.0  16912 12288 ?        S    {start:<5}   0:00 {proc}"
        for invert, rx in filters:
            if bool(rx.search(new)) == invert:
                return None
        pos = rows[-1][0] + 1
        for i, m in rows:
            if int(m.group(2)) > pid:
                pos = i
                break
        lines.insert(pos, new)
        return "\n".join(lines)

    rewrite_outputs(ctx, fn, tools=("exec_shell",))


# ---- fault_not_realized ----------------------------------------------------------


def _absent(row: dict, evidence: dict | None = None) -> dict:
    d = row["data"]
    d["result"] = "absent"
    d["error"] = "fault artifact absent"
    if evidence is not None:
        d["evidence"] = evidence
    row["message"] = f"Fault artifact absent: phase={d['phase']} fault={d['fault']}"
    return row


def _contradicted(ctx: TrialCtx, rx: re.Pattern) -> bool:
    steps, _ = parse_messages(ctx.src / "messages.jsonl")
    return any(s.kind == "tool_result" and rx.search(s.text) for s in steps)


def p_register_reset(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """Fabric manager re-pushes switch defaults after injection, clearing the register write."""
    if _contradicted(ctx, plant.params["evidence_rx"]):
        ctx.notes.append("tool output shows the artifact; trial left unchanged")
        return
    doc = Jsonl(ctx.text("nika.jsonl"))
    host = plant.params["host"]
    agent_i = None
    changed = False
    for i in range(len(doc)):
        ln = doc.lines[i]
        if '"agent_start"' in ln and agent_i is None:
            agent_i = i
        if '"fault_presence_recheck"' not in ln:
            continue
        row = doc.try_row(i)
        if not isinstance(row, dict):
            continue
        d = row.get("data") or {}
        if (
            d.get("phase") in ("during_agent", "before_cleanup")
            and d.get("result") == "present"
        ):
            ev = dict(d.get("evidence") or {})
            ev["observed_threshold"] = None
            doc.set_row(i, _absent(row, ev))
            changed = True
    if not changed or agent_i is None:
        return
    from datetime import datetime, timedelta

    start = doc.row(agent_i)
    ts = (
        datetime.fromisoformat(start["timestamp"]) - timedelta(seconds=0.12)
    ).isoformat(timespec="microseconds")
    sysrow = {
        "timestamp": ts,
        "level": "INFO",
        "event": "system",
        "message": f"P4Runtime arbitration regained on {host}; re-pushed pipeline config and default port registers",
    }
    style = line_style(doc.lines[agent_i])
    doc.lines.insert(agent_i, dump_line(sysrow, style))
    # Re-index cached rows after the insertion point.
    doc._rows = {(k + 1 if k >= agent_i else k): v for k, v in doc._rows.items()}
    doc.dirty = {(k + 1 if k >= agent_i else k) for k in doc.dirty}
    ctx.put("nika.jsonl", doc.text())


def p_late_injection(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """Injection call timed out, was retried, and landed after the agent finished."""
    if _contradicted(ctx, plant.params["evidence_rx"]):
        ctx.notes.append("tool output shows the artifact; trial left unchanged")
        return
    text = ctx.text("nika.jsonl")
    doc = Jsonl(text)
    rows = [doc.try_row(i) for i in range(len(doc))]
    ev = [r.get("event") if r else None for r in rows]
    try:
        a_start = ev.index("agent_start")
        a_end = ev.index("agent_end")
    except ValueError:
        return
    inj_events = {
        "system",
        "failure_injected",
        "failure_verified",
        "failure_inject_complete",
        "ground_truth_saved",
        "remote_failure_inject",
    }
    block = [
        i
        for i in range(a_start)
        if ev[i] in inj_events
        or (
            ev[i] == "fault_presence_recheck"
            and (rows[i]["data"] or {}).get("phase") == "post_inject"
        )
    ]
    # Keep 'system' rows only when they describe the injection itself.
    block = [
        i for i in block if ev[i] != "system" or "Inject" in str(rows[i].get("message"))
    ]
    if not block or "failure_injected" not in [ev[i] for i in block]:
        return
    from datetime import datetime, timedelta

    def parse(ts: str) -> datetime:
        return datetime.fromisoformat(ts)

    t0 = parse(rows[block[0]]["timestamp"])
    t_end = parse(rows[a_end]["timestamp"])
    base = t_end + timedelta(seconds=1.7)
    moved = []
    for i in block:
        r = copy.deepcopy(rows[i])
        r["timestamp"] = (base + (parse(r["timestamp"]) - t0)).isoformat(
            timespec="microseconds"
        )
        moved.append((i, r))
    # During-agent recheck: artifact not there yet.
    for i in range(a_start, a_end):
        if ev[i] == "fault_presence_recheck" and (rows[i]["data"] or {}).get(
            "phase"
        ) in ("during_agent", "before_cleanup"):
            r = copy.deepcopy(rows[i])
            evid = {
                k: (None if k.startswith("observed") else v)
                for k, v in (r["data"].get("evidence") or {}).items()
            }
            r = _absent(r, evid)
            rows[i] = r
            doc.set_row(i, r)
    retry = {
        "timestamp": (base - timedelta(seconds=0.6)).isoformat(timespec="microseconds"),
        "level": "WARNING",
        "event": "system",
        "message": "failure injection request timed out after 120s; retrying once",
    }
    styles = {
        i: line_style(doc.lines[i]) for i in range(len(doc)) if rows[i] is not None
    }
    new_lines = []
    block_set = set(i for i, _ in moved)
    for i in range(len(doc)):
        if i in block_set:
            continue
        line = (
            doc.lines[i] if i not in doc.dirty else dump_line(doc._rows[i], styles[i])
        )
        new_lines.append(line)
        if i == a_end:
            new_lines.append(dump_line(retry, styles.get(i, True)))
            for j, r in moved:
                new_lines.append(dump_line(r, styles[j]))
    ctx.put("nika.jsonl", "\n".join(new_lines) + ("\n" if doc.trailing else ""))


def p_workload_stopped(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """Traffic workload that the fault needs exits before the agent starts."""
    doc = Jsonl(ctx.text("nika.jsonl"))
    start_msg = plant.params["start_msg"]
    a_start = None
    found = False
    for i in range(len(doc)):
        ln = doc.lines[i]
        if start_msg in ln:
            found = True
        if '"agent_start"' in ln and a_start is None:
            a_start = i
    if not found or a_start is None:
        return
    from datetime import datetime, timedelta

    ts = datetime.fromisoformat(doc.row(a_start)["timestamp"]) - timedelta(seconds=0.21)
    row = {
        "timestamp": ts.isoformat(timespec="microseconds"),
        "level": "INFO",
        "event": "system",
        "message": plant.params["stop_msg"],
    }
    doc.lines.insert(a_start, dump_line(row, line_style(doc.lines[a_start])))
    doc._rows = {(k + 1 if k >= a_start else k): v for k, v in doc._rows.items()}
    ctx.put("nika.jsonl", doc.text())


# ---- signal_invisible -------------------------------------------------------------


def p_output_cap(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """Tool output limit for this tool is set far below the harness default."""
    cap, head, tail, tool = (plant.params[k] for k in ("cap", "head", "tail", "tool"))

    def fn(call, inp, out):
        if is_overlimit(out) or len(out) <= cap:
            return None
        return overlimit(out, head, tail)

    rewrite_outputs(ctx, fn, guard_removed, tools=(tool,))


def p_tool_missing(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """Client image lacks the DNS lookup tools."""
    host_rx = re.compile(plant.params["host_rx"])
    tools = "|".join(plant.params["tools"])
    single = re.compile(
        r"^\s*("
        + tools
        + r")\b[^;&|`$()]*?(\s*2>&1)?\s*(\|\s*(head|tail|grep)\b[^;&|`$()]*)?\s*$"
    )

    def fn(call, inp, out):
        cmd = str(inp.get("command") or "")
        m = single.match(cmd)
        if not m or not host_rx.search(host_of(inp)) or "not found" in out:
            return None
        return f"/bin/bash: line 1: {m.group(1)}: command not found"

    def guard(old, new):
        toks = re.findall(r"^Address:\s*(\d+\.\d+\.\d+\.\d+)\s*$", old, re.M)
        toks += re.findall(r"\sIN\s+A\s+(\d+\.\d+\.\d+\.\d+)", old)
        toks += re.findall(r"^(\d+\.\d+\.\d+\.\d+)\s*$", old, re.M)
        toks += re.findall(r"status: (\w+)", old)
        return sorted(set(t for t in toks if t != "NOERROR"))

    rewrite_outputs(ctx, fn, guard, tools=("exec_shell",))


def p_sample_clamp(ctx: TrialCtx, plant: Plant, shared: dict) -> None:
    """HTTP probe silently runs a single request whatever 'times' asks for."""

    def fn(call, inp, out):
        try:
            times = int(inp.get("times") or 1)
        except (TypeError, ValueError):
            return None
        lines = [ln for ln in out.split("\n") if ln.startswith("namelookup:")]
        if times <= 1 or len(lines) <= 1 or is_overlimit(out):
            return None
        first = out.split("\n")[0]
        if not first.startswith("namelookup:"):
            return None
        return first

    rewrite_outputs(ctx, fn, guard_removed, tools=("curl_web_test",))


# --------------------------------------------------------------------------
# Plant catalogue


PLANTS: list[Plant] = [
    Plant(
        "P01",
        "grading_error",
        "Scorer grades the task's fault type through a stale alias",
        "The scoring alias table maps p4_table_entry_misconfig to p4_table_entry_missing, so recorded scores "
        "credit the sibling label and give zero type credit for the correct one; ground_truth.json is correct.",
        ["p4_dc_gateway__p4_table_entr__hb84a5ff985c3"],
        ["p4_dc_gateway__p4runtime_pip__hd91a418d6945"],
        ["eval_metrics.json", "run.json:eval_metrics"],
        p_alias_label,
        {
            "fault_type": "p4_table_entry_misconfig",
            "graded_as": "p4_table_entry_missing",
        },
    ),
    Plant(
        "P02",
        "label_unidentifiable",
        "Ground-truth link id not in canonical endpoint order",
        "The ground-truth builder writes the faulted link with endpoints in injection order "
        "(isp1_core first) instead of the sorted canonical id used by the resource inventory, so no "
        "submittable link id matches; scores are consistent with the mutated ground truth.",
        ["enterprise_branch__link_pack__ha7c5a53baacc"],
        ["enterprise_branch__link_down__hcecf68b8564f"],
        [
            "ground_truth.json:root_causes[].resource",
            "eval_metrics.json",
            "run.json:eval_metrics",
        ],
        p_link_order,
    ),
    Plant(
        "P03",
        "harness_defect",
        "exec_shell returns empty output for one device",
        "Container exec on super_spine_router_0 loses stdout: every exec_shell call on that device "
        "returns an empty successful result (calls whose output the agent later quoted are left unchanged).",
        ["dc_clos__link_detach__m__h240c6b1dc30f"],
        ["dc_clos__arp_acl_block__m__heebcdfb08b4d"],
        ["messages.jsonl:tool results"],
        p_empty_host,
        {"host": "super_spine_router_0"},
    ),
    Plant(
        "P04",
        "run_validity",
        "Task-level override lowers max_tokens",
        "A task-level agent override sets a smaller max_tokens for this task's trials only, so they run "
        "with a different generation budget from the rest of the run.",
        ["sdn_l3_clos__flow_rule_shado__he6582a3b1d4e"],
        ["sdn_l3_clos__arp_cache_poiso__hb377eed72e6e"],
        [
            "run.json:max_tokens",
            "messages.jsonl:llm_start.model.kwargs.extra_body.max_tokens",
        ],
        p_max_tokens,
    ),
    Plant(
        "P05",
        "shortcut",
        "Injected nftables rules carry the fault name as comment",
        "The ACL injector tags its drop rules with a comment holding the fault type, visible in "
        "'nft list ruleset' output on the faulted router.",
        ["campus_lan__ospf_acl_block__h6251e03d65a2"],
        ["campus_lan__ospf_neighbor_mi__hfcf4b0494422"],
        ["messages.jsonl:tool results"],
        p_rule_comment,
        {"rule": "ip protocol ospf drop", "tag": "ospf_acl_block"},
    ),
    Plant(
        "P06",
        "healthy_lure",
        "Unestablished BGP peer for an undeployed leaf",
        "spine_router_0_0 carries a configured peer for a leaf that is not deployed; it stays in Active "
        "state in 'show bgp summary'. By design, but the brief does not say so.",
        ["dc_clos__healthy__s"],
        ["dc_clos__healthy__m"],
        ["messages.jsonl:tool results"],
        p_idle_peer,
        {"host": "spine_router_0_0", "peer": "172.16.0.13", "asn": 65202},
    ),
    Plant(
        "P07",
        "fault_not_realized",
        "Register write cleared by pipeline re-push",
        "After injection the fabric manager regains P4Runtime arbitration on spine_1 and re-pushes "
        "default port registers, so the ECN threshold write is gone before the agent starts; the presence "
        "rechecks report the artifact absent.",
        ["p4_dc_gateway__p4_ecn_thresh__h4bf62883ca88"],
        ["p4_dc_gateway__p4_tcam_entry__hfd952409a6c1"],
        ["nika.jsonl:fault_presence_recheck", "nika.jsonl:system (inserted)"],
        p_register_reset,
        {
            "host": "spine_1",
            "evidence_rx": re.compile(
                r"(?i)(ecn|threshold)[^\n]{0,200}\b1024\b|\b1024\b[^\n]{0,200}(?i:ecn|threshold)"
            ),
        },
    ),
    Plant(
        "P08",
        "grading_error",
        "Multi-root-cause task graded with any-match",
        "The task's scoring treats a submission as fully correct when any one of the three ground-truth "
        "pairs (or resources) matches, rewarding partial answers with full credit.",
        ["enterprise_branch__wireguard__hcd29627d2190"],
        ["enterprise_branch__host_stat__h693056222d8b"],
        ["eval_metrics.json", "run.json:eval_metrics"],
        p_any_match,
    ),
    Plant(
        "P09",
        "signal_invisible",
        "p4rt_exec output limit far below the harness default",
        "The output limit for p4rt_exec in this task is 4000 characters instead of ~10000, so table and "
        "group dumps come back as the over-limit warning with short head/tail excerpts.",
        ["p4_dc_gateway__p4_ecmp_group__hee7f0e686145"],
        ["p4_dc_gateway__p4runtime_par__h92346d5301fa"],
        ["messages.jsonl:tool results"],
        p_output_cap,
        {"tool": "p4rt_exec", "cap": 4000, "head": 1800, "tail": 1800},
    ),
    Plant(
        "P10",
        "harness_privilege",
        "Lab administration MCP server exposed to the agent",
        "The scenario's MCP registry also starts a lab administration server whose tools give host-level "
        "access to devices; it appears in the agent's MCP configuration and tool list.",
        ["enterprise_branch__bgp_missi__h3315430073e8"],
        ["enterprise_branch__host_stat__h693056222d8b"],
        [
            "messages.jsonl:mcp_config.servers",
            "messages.jsonl:claude init tools/mcp_servers",
        ],
        p_extra_mcp,
        {
            "server": "lab_admin_mcp_server",
            "tools": ["device_console", "lab_snapshot", "restart_device"],
        },
    ),
    Plant(
        "P11",
        "label_unidentifiable",
        "Probe observer listed as a second root cause",
        "The ground-truth builder adds the probe observer device (client_1_1) as a root cause next to the "
        "attacked web server; nothing is wrong on the observer, so that pair cannot be identified.",
        ["sdn_l3_clos__web_dos_attack__hb8cb4f83387a"],
        ["sdn_l3_clos__arp_cache_poiso__hb377eed72e6e"],
        ["ground_truth.json:root_causes", "eval_metrics.json", "run.json:eval_metrics"],
        p_append_observer,
        {"observer": "client_1_1"},
    ),
    Plant(
        "P12",
        "harness_defect",
        "Submit succeeds but drops non-ethN interface ids",
        "The submission normaliser validates interface names against ethN and silently drops other root "
        "causes (e.g. WireGuard interfaces), while the submit tool still answers 'Submission success.'.",
        ["enterprise_branch__wireguard__hffb08b156bdd"],
        ["enterprise_branch__tcp_recei__h131c3a680384"],
        ["submission.json:root_causes", "eval_metrics.json", "run.json:eval_metrics"],
        p_submit_drop,
    ),
    Plant(
        "P13",
        "run_validity",
        "Task-level override shortens step and time budget",
        "A task-level override gives this task's agent runs max_steps 40 and a 600 s timeout instead of "
        "the run's settings.",
        ["enterprise_branch__snat_port__h52fb6c69e2af"],
        ["enterprise_branch__nat_mappi__h9a2736339326"],
        ["nika.jsonl:agent_start.data.max_steps/timeout_sec"],
        p_budget,
        {"max_steps": 40, "timeout_sec": 600},
    ),
    Plant(
        "P14",
        "shortcut",
        "Injector leaves the original option commented out",
        "The DHCP gateway injector comments out the original 'option routers' line and adds the spoofed "
        "one below it, so dhcpd.conf shows the correct and the injected value side by side.",
        ["campus_lan__dhcp_spoofed_gat__ha283c5609ad2"],
        ["campus_lan__dhcp_spoofed_dns__h4ee0ce80b947"],
        ["messages.jsonl:tool results"],
        p_commented_original,
    ),
    Plant(
        "P15",
        "grading_error",
        "Trials of a task share one scorecard",
        "Scores are written to a case-level path, so every trial of the task carries the last trial's "
        "eval_metrics (identical scores, steps and tokens) regardless of its own submission.",
        ["campus_lan__ospf_area_miscon__h7ddf09bc0f6e"],
        ["campus_lan__ospf_neighbor_mi__hfcf4b0494422"],
        ["eval_metrics.json", "run.json:eval_metrics"],
        p_shared_scorecard,
    ),
    Plant(
        "P16",
        "fault_not_realized",
        "Injection retried after the agent finished",
        "The injection request timed out and the retry landed after agent_end; during the agent run the "
        "fault was not present (the during_agent recheck reports it absent).",
        ["p4_dc_gateway__int_insuffici__h65e77e85f180"],
        ["p4_dc_gateway__icmp_frag_nee__ha855b5fd985e"],
        [
            "nika.jsonl:failure_* / ground_truth_saved / post_inject recheck (moved)",
            "nika.jsonl:system (inserted)",
            "nika.jsonl:during_agent recheck",
        ],
        p_late_injection,
        {"evidence_rx": re.compile(r"\b1480\b")},
    ),
    Plant(
        "P17",
        "healthy_lure",
        "Controller keeps a decommissioned switch as unavailable device",
        "The ONOS store keeps a switch that is no longer part of the fabric; /onos/v1/devices lists it "
        "with available=false. By design, but the brief does not say so.",
        ["sdn_l3_clos__healthy__m"],
        ["sdn_l3_clos__healthy__l"],
        ["messages.jsonl:tool results"],
        p_stale_device,
        {
            "device": {
                "id": "of:0000000000003001",
                "available": False,
                "role": "NONE",
                "chassisId": "3001",
                "lastUpdate": "1791251287112",
                "humanReadableLastUpdate": "disconnected 47m ago",
                "annotations": {
                    "channelId": "172.31.0.40:46018",
                    "managementAddress": "172.31.0.40",
                },
            }
        },
    ),
    Plant(
        "P18",
        "out_of_scope",
        "Harness FRR guard process visible on lab routers",
        "The harness runs an FRR guard process inside every router, with its state file path on the command line; "
        "agents listing processes see harness machinery outside the network under test.",
        ["isp_germany50__frr_service_d__h4af1d78aea8d"],
        ["isp_dfn-bwin_ebgp_rtbh__bgp__hb65dab9be18e"],
        ["messages.jsonl:tool results"],
        p_guard_process,
        {
            "command": "/usr/bin/python3 /opt/labctl/frr_guard.py --interval 5 --state /var/lib/labctl/frr_guard.json",
            "pid": 31,
        },
    ),
    Plant(
        "P19",
        "label_unidentifiable",
        "Node-level fault labelled with a port",
        "The ground truth for a missing table entry names leaf_1's host port (the injector's port "
        "parameter) instead of the switch; agents naming the switch cannot match an interface label.",
        ["p4_dc_fabric__p4_table_entry__h3bb65a370906"],
        ["p4_dc_gateway__bmv2_switch_d__h8e9f694d6699"],
        [
            "ground_truth.json:root_causes[].resource",
            "eval_metrics.json",
            "run.json:eval_metrics",
        ],
        p_granularity,
        {"node": "leaf_1", "intf": "eth1"},
    ),
    Plant(
        "P20",
        "harness_defect",
        "srl_exec_cli output silently truncated",
        "The SR Linux CLI tool returns at most 2048 bytes and drops the rest without any marker.",
        ["min3clos__bgp_asn_misconfig__h72c7bf7e08f2"],
        ["isp_pdh__bgp_acl_block__s__i__hbe4170621772"],
        ["messages.jsonl:tool results"],
        p_silent_truncation,
        {"tool": "srl_exec_cli", "cap": 2048},
    ),
    Plant(
        "P21",
        "signal_invisible",
        "DNS client tools missing on the lab PCs",
        "The PC image used by this task lacks dig/nslookup, so lookups from the clients fail with "
        "'command not found' and the wrong record is only visible from other devices.",
        ["campus_lan__dns_record_error__h139fe82d0c0f"],
        ["campus_lan__dns_service_down__h88cd16fc8e7e"],
        ["messages.jsonl:tool results"],
        p_tool_missing,
        {"host_rx": r"^pc_", "tools": ["dig", "nslookup", "host"]},
    ),
    Plant(
        "P22",
        "run_validity",
        "Third trial is a copy of the second",
        "The result publisher resolved t03 to t02's session, so t03 holds t02's files (same session id, "
        "trace and scores); the task has two independent trials, not three.",
        ["sdn_l3_clos__sdn_controller__h7b118290aaf1"],
        ["sdn_l3_clos__flow_rule_loop__hebae33c8d7d4"],
        ["all files of t03"],
        p_duplicate_trial,
    ),
    Plant(
        "P23",
        "shortcut",
        "Brief carries a note derived from the injection parameters",
        "The task brief template appends a monitoring note naming the faulted port (gateway_1:eth1).",
        ["p4_dc_gateway__silent_egress__h8eba7ba89f0d"],
        ["p4_dc_gateway__tcp_syn_flood__hc408eaaa9f62"],
        ["run.json:task_description", "messages.jsonl:prompt (diagnosis)"],
        p_prompt_hint,
        {"note": "Egress monitoring is enabled on gateway_1:eth1."},
    ),
    Plant(
        "P24",
        "grading_error",
        "Fault types matched on their family prefix",
        "The task's scorer compares fault types on their first two words, so p4_table_entry_missing or "
        "p4_table_entry_misconfig earn full credit for p4_table_resource_exhaustion.",
        ["p4_dc_gateway__p4_table_reso__h2bab5479b819"],
        ["p4_dc_gateway__icmp_acl_bloc__h2ed8bf2882e3"],
        ["eval_metrics.json", "run.json:eval_metrics"],
        p_prefix_match,
    ),
    Plant(
        "P25",
        "healthy_lure",
        "BGP aggregate discard route on a super-spine",
        "super_spine_router_0 originates a summary with a discard (blackhole) route; the kernel table shows "
        "a blackhole for the pod aggregate. By design, but the brief does not say so.",
        ["dc_clos__healthy__l"],
        ["dc_clos__healthy__m"],
        ["messages.jsonl:tool results"],
        p_aggregate_blackhole,
        {
            "host": "super_spine_router_0",
            "route": "blackhole 10.0.0.0/14 proto bgp metric 20 ",
        },
    ),
    Plant(
        "P26",
        "fault_not_realized",
        "Traffic workload exits before the agent starts",
        "The DSCP remark only matters for the CORP QoS workload, which stops right after injection; the "
        "remark rule stays but no affected traffic flows during the agent run.",
        ["enterprise_branch__vrf_dscp__hcd608018d99c"],
        ["enterprise_branch__tcp_recei__h131c3a680384"],
        ["nika.jsonl:system (inserted)"],
        p_workload_stopped,
        {
            "start_msg": "Started CORP QoS compete workload on hq_edge/wg_br1",
            "stop_msg": "Stopped CORP QoS compete workload on hq_edge/wg_br1: 4 flows exited (rc=0)",
        },
    ),
    Plant(
        "P27",
        "harness_defect",
        "k8s_list_events returns an empty list",
        "The k8s event tool queries the wrong cluster context for this task and answers with an empty list "
        "instead of an error.",
        ["llmd_lab__host_missing_ip__h235346563fb5"],
        ["llmd_lab__host_ip_conflict__h7172cc7c30cf"],
        ["messages.jsonl:tool results"],
        p_events_empty,
    ),
    Plant(
        "P28",
        "label_unidentifiable",
        "Ontology describes the label like its sibling",
        "The submission context for this task renders southbound_port_block with the description of "
        "southbound_port_mismatch, so the two labels read the same.",
        ["sdn_l3_clos__southbound_port__hc71cfcc62969"],
        ["sdn_l3_clos__healthy__l"],
        ["messages.jsonl:prompt (submission)"],
        p_ontology_dup,
        {
            "label": "southbound_port_block",
            "desc": "Controller southbound channel port is blocked.",
            "sibling_desc": "Controller southbound listen port mismatches switch config.",
        },
    ),
    Plant(
        "P29",
        "harness_privilege",
        "Lab router bridged to the host container network",
        "server_access_router gets an extra interface on the host's container bridge (Kathara bridged "
        "option), giving the lab a path outside the benchmark network.",
        ["campus_lan__dns_lookup_laten__h9afd41613374"],
        ["campus_lan__dns_service_down__h88cd16fc8e7e"],
        ["messages.jsonl:tool results"],
        p_bridged_mgmt,
        {
            "host": "server_access_router",
            "intf": "eth8",
            "addr": "172.17.0.3/16",
            "brd": "172.17.255.255",
            "net": "172.17.0.0/16",
            "mac": "02:42:ac:11:00:03",
        },
    ),
    Plant(
        "P30",
        "signal_invisible",
        "HTTP probe runs one request regardless of 'times'",
        "curl_web_test ignores its 'times' argument for this task and returns a single sample, so "
        "intermittent slow responses are rarely seen.",
        ["llmd_lab__sender_resource_co__h1bbe56e6f9c3"],
        ["llmd_lab__host_ip_conflict__h7172cc7c30cf"],
        ["messages.jsonl:tool results"],
        p_sample_clamp,
    ),
]


# --------------------------------------------------------------------------
# Driver


SHADOW = OUT / ".links"


def disguise(src_file: Path, dst_file: Path, run: str, trial: str) -> None:
    """Make a rewritten file look like its untouched neighbours on disk.

    Untouched files in the dataset are hard links (link count 2) carrying the
    original timestamps. A rewritten file gets the source file's timestamps
    and a second link outside the dataset, so neither ``ls -l`` link counts
    nor modification times single out planted files.
    """
    ref = src_file if src_file.exists() else dst_file
    st = os.stat(ref)
    os.utime(dst_file, ns=(st.st_atime_ns, st.st_mtime_ns))
    shadow = SHADOW / run / trial / dst_file.name
    shadow.parent.mkdir(parents=True, exist_ok=True)
    if shadow.exists():
        shadow.unlink()
    os.link(dst_file, shadow)


def resolved_runs() -> dict[str, Path]:
    return {
        p.name: Path(os.path.realpath(p)) for p in sorted(SRC.iterdir()) if p.is_dir()
    }


def build() -> dict[str, Any]:
    runs = resolved_runs()
    if DST.exists():
        shutil.rmtree(DST)
    if SHADOW.exists():
        shutil.rmtree(SHADOW)
    DST.mkdir(parents=True)
    for name, real in runs.items():
        subprocess.run(["cp", "-al", str(real), str(DST / name)], check=True)

    all_tasks = [t for p in PLANTS for t in p.tasks]
    assert len(all_tasks) == len(set(all_tasks)), "plants must hit distinct tasks"
    assert not set(all_tasks) & {c for p in PLANTS for c in p.controls}, (
        "controls must be untouched"
    )

    checksums: dict[str, dict[str, str]] = {}
    report: dict[str, Any] = {}
    for plant in PLANTS:
        CURRENT_PLANT["id"] = plant.id
        per_run: dict[str, list[str]] = {}
        notes: dict[str, list[str]] = defaultdict(list)
        for run, real in runs.items():
            per_run[run] = []
            for task in plant.tasks:
                trials = sorted(
                    p
                    for p in (real / "trials").iterdir()
                    if p.name.startswith(task + "__t")
                )
                shared: dict[str, Any] = {}
                if trials:
                    shared["last_trial"] = trials[-1]
                for tdir in trials:
                    ctx = TrialCtx(
                        run, tdir.name, tdir, DST / run / "trials" / tdir.name
                    )
                    plant.apply(ctx, plant, shared)
                    if ctx.notes:
                        notes[f"{run}/{tdir.name}"].extend(ctx.notes)
                    if not ctx.pending:
                        continue
                    for fname, text in sorted(ctx.pending.items()):
                        src_file = tdir / fname
                        dst_file = ctx.dst / fname
                        key = str(src_file)
                        if src_file.exists():
                            checksums[key] = {"before": sha256(src_file)}
                        if dst_file.exists() or dst_file.is_symlink():
                            dst_file.unlink()  # break the hard link; never write through it
                        dst_file.write_text(text, encoding="utf-8")
                        disguise(src_file, dst_file, run, tdir.name)
                    st = os.stat(tdir)
                    os.utime(ctx.dst, ns=(st.st_atime_ns, st.st_mtime_ns))
                    per_run[run].append(tdir.name)
        report[plant.id] = {"per_run": per_run, "notes": dict(notes)}
        if plant.id in REWRITE_STATS:
            report[plant.id]["tool_results"] = dict(REWRITE_STATS[plant.id])
    for key, row in checksums.items():
        row["after"] = sha256(Path(key))
    return {"report": report, "checksums": checksums}


def write_outputs(result: dict[str, Any]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    import yaml

    plants = []
    rows = []
    for plant in PLANTS:
        rep = result["report"][plant.id]
        per_run = {r: v for r, v in rep["per_run"].items()}
        n_runs = sum(1 for v in per_run.values() if v)
        n_trials = sum(len(v) for v in per_run.values())
        plants.append(
            {
                "id": plant.id,
                "class": plant.cls,
                "title": plant.title,
                "mechanism": plant.mechanism,
                "affected_tasks": plant.tasks,
                "control_tasks": plant.controls,
                "mutated": plant.files,
                "n_runs_affected": n_runs,
                "n_trials_affected": n_trials,
                **(
                    {"tool_results": rep["tool_results"]}
                    if "tool_results" in rep
                    else {}
                ),
                "mutated_trials": per_run,
                **({"notes": rep["notes"]} if rep["notes"] else {}),
            }
        )
        rows.append((plant.id, plant.cls, plant.title, n_runs, n_trials))
    changed = sum(1 for v in result["checksums"].values() if v["before"] != v["after"])
    manifest = {
        "dataset": str(DST),
        "source": str(SRC),
        "generator": "experiment/audit_evolution/plant.py",
        "n_plants": len(PLANTS),
        "original_files_checked": len(result["checksums"]),
        "original_files_changed": changed,
        "plants": plants,
    }
    (OUT / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False, width=110)
    )
    (OUT / "checksums.json").write_text(
        json.dumps(result["checksums"], indent=1, sort_keys=True)
    )
    lines = [
        "# Planted defects (nika-0.2.0-planted)",
        "",
        f"Dataset: `{DST}`. Source: `{SRC}`. Manifest: `manifest.yaml`.",
        "",
        "| ID | Class | Title | Runs | Trials |",
        "| --- | --- | --- | ---: | ---: |",
    ]
    for pid, cls, title, nr, nt in rows:
        lines.append(f"| {pid} | {cls} | {title} | {nr} | {nt} |")
    by_cls: dict[str, int] = defaultdict(int)
    for _, cls, _, _, _ in rows:
        by_cls[cls] += 1
    lines += [
        "",
        "Plants per class: " + ", ".join(f"{k} {v}" for k, v in sorted(by_cls.items())),
        "",
    ]
    (OUT / "summary.md").write_text("\n".join(lines))


def verify(cli: bool) -> int:
    bad = 0
    runs = resolved_runs()
    for run in sorted(p for p in DST.iterdir() if p.is_dir()):
        n = 0
        for t in trial_dirs(run):
            tr = load_trial(t)
            orig = load_trial(runs[run.name] / "trials" / t.name)
            ok = (
                tr.bad_lines == orig.bad_lines
                and bool(tr.run)
                and bool(tr.steps) == bool(orig.steps)
            )
            if not ok:
                print(
                    f"LOAD PROBLEM {run.name}/{t.name}: bad_lines={tr.bad_lines} (orig {orig.bad_lines})"
                )
                bad += 1
            n += 1
        print(f"{run.name}: {n} trials load")
    if cli:
        for run in sorted(p for p in DST.iterdir() if p.is_dir()):
            out = ROOT / "tmp" / "plant" / "cli" / f"{run.name}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                [
                    str(PYTHON),
                    "-m",
                    "nika.audit.cli",
                    "run",
                    str(run),
                    "--out",
                    str(out),
                ],
                capture_output=True,
                text=True,
                cwd=str(ROOT / "sandbox" / "tree"),
            )
            print(f"cli run {run.name}: rc={proc.returncode}")
            if proc.returncode != 0:
                print(proc.stderr[-2000:])
                bad += 1
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--verify", action="store_true", help="Load every trial and run the audit CLI."
    )
    ap.add_argument(
        "--no-build", action="store_true", help="Only verify an existing dataset."
    )
    args = ap.parse_args()
    if not args.no_build:
        result = build()
        write_outputs(result)
        changed = [
            k for k, v in result["checksums"].items() if v["before"] != v["after"]
        ]
        print(f"originals checked: {len(result['checksums'])}, changed: {len(changed)}")
        for pid, rep in result["report"].items():
            print(
                pid,
                sum(len(v) for v in rep["per_run"].values()),
                "trials",
                rep.get("tool_results", ""),
            )
        if changed:
            return 1
    if args.verify:
        return 1 if verify(cli=True) else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
