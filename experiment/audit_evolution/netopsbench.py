"""Convert published NetOpsBench agent traces into NIKA evaluation runs.

Input: the Hugging Face dataset ``yyyyyt/netopsbench-trace`` downloaded to
``<ROOT>/data/netopsbench-raw`` with every ``.tar.zst`` extracted into a
sibling directory of the same stem. Output: ``<ROOT>/data/netopsbench/<run>/``
with ``identity.yaml``, ``metadata.yaml`` and ``trials/<trial>/`` holding
``messages.jsonl``, ``run.json``, ``submission.json``, ``ground_truth.json``,
``eval_metrics.json`` and the source ``netopsbench_result.json``, plus
``<ROOT>/data/netopsbench/mapping.jsonl`` (trial -> source files).

One run per published archive (model, scale, release). One trial per case.

Mapping (ATIF-v1.7 step -> messages.jsonl row):
  first LLM request (system + human)  -> ``prompt``
  ``llm`` step                         -> ``llm_end`` (content, finish_reason, usage)
  ``tool_call`` step                   -> ``tool_start`` + ``tool_end``/``tool_error``
  ``error`` step                       -> ``agent_error``
  initial context, ``final_diagnosis`` -> kept as ``initial_context`` and
                                          ``final_diagnosis`` rows; the audit
                                          parser does not turn them into steps.

The traces do not record the NetOpsBench commit. ``nika_git_commit`` holds the
commit inferred from the upstream history: the latest commit (any ref, by
committer date) at or before the first trial start of the trace's source run.
``commit_basis`` says so.

Usage:
  python -I netopsbench.py convert --git <netopsbench mirror .git> [--root DIR]
"""

from __future__ import annotations

import argparse
import bisect
import collections
import csv
import datetime as dt
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path("/workspace/nika-audit-evolution")
HF_REPO = "yyyyyt/netopsbench-trace"
GITHUB = "https://github.com/NetX-lab/NetOpsBench"
COMMIT_BASIS = (
    "inferred: latest upstream commit (committer date) at or before the first "
    "trial start of the source run; traces do not record a commit"
)


def _ts(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


class Commits:
    """Upstream commits by committer date, for timestamp inference."""

    def __init__(self, git_dir: Path) -> None:
        out = subprocess.run(
            ["git", "--git-dir", str(git_dir), "log", "--all", "--format=%H %cI"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        rows = sorted((_ts(d), h) for h, d in (ln.split() for ln in out.splitlines()))
        self.dates = [d for d, _ in rows]
        self.shas = [h for _, h in rows]
        self.git_dir = git_dir

    def at(self, when: dt.datetime) -> str:
        i = bisect.bisect_right(self.dates, when)
        if not i:
            raise ValueError(f"no commit before {when}")
        return self.shas[i - 1]

    def version(self, sha: str) -> str | None:
        text = subprocess.run(
            ["git", "--git-dir", str(self.git_dir), "show", f"{sha}:pyproject.toml"],
            capture_output=True,
            text=True,
        ).stdout
        m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
        return m.group(1) if m else None


# ---------------------------------------------------------------- trial parts


def _tool_output(raw: str) -> tuple[str, dict[str, Any]]:
    """Text the agent saw from one serialized LangChain ToolMessage."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        # The trace writer cut the serialized message (``...<truncated N chars>``).
        m = re.search(r"<truncated (\d+) chars>$", raw)
        return raw, {
            "trace_truncated": True,
            "truncated_chars": int(m.group(1)) if m else None,
        }
    content = msg.get("content")
    if isinstance(content, list):
        text = "\n".join(
            str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in content
        )
    elif content is None:
        text = ""
    else:
        text = str(content)
    meta = {
        "status": msg.get("status"),
        "provider_tool_call_id": msg.get("tool_call_id"),
    }
    return text, meta


def _prompt(step: dict[str, Any]) -> str:
    parts = []
    for m in step["extra"]["llm_request"]["messages"][:2]:
        content = m.get("content")
        if isinstance(content, list):
            content = "\n".join(
                str(b.get("text", "")) if isinstance(b, dict) else str(b)
                for b in content
            )
        parts.append(f"[{m.get('role')}]\n{content}")
    return "\n\n".join(parts)


def messages(atif: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    phase = "diagnosis"
    steps = atif["steps"]
    first_llm = next(
        (s for s in steps if s["extra"].get("type") in ("llm", "error")), None
    )
    for step in steps:
        extra = step.get("extra") or {}
        kind = extra.get("type")
        base = {"phase": phase, "atif_step_id": step.get("step_id")}
        if extra.get("is_initial_context"):
            rows.append(
                {
                    **base,
                    "timestamp": step.get("timestamp"),
                    "event": "initial_context",
                    "text": step.get("message"),
                }
            )
            continue
        if step is first_llm:
            rows.append(
                {
                    **base,
                    "timestamp": extra.get("started_at"),
                    "event": "prompt",
                    "text": _prompt(step),
                }
            )
        if kind == "llm":
            resp = extra.get("llm_response") or {}
            meta = resp.get("response_metadata") or {}
            rows.append(
                {
                    **base,
                    "timestamp": extra.get("ended_at"),
                    "started_at": extra.get("started_at"),
                    "event": "llm_end",
                    "run_id": extra.get("run_id"),
                    "parent_run_id": extra.get("parent_run_id"),
                    "model": extra.get("model"),
                    "text": resp.get("content") or "",
                    "generation_info": {"finish_reason": meta.get("finish_reason")},
                    "usage_metadata": resp.get("usage_metadata"),
                    "tool_calls": resp.get("tool_calls") or [],
                }
            )
        elif kind == "tool_call":
            calls = step.get("tool_calls") or [{}]
            call = calls[0]
            call_id = extra.get("tool_call_id") or call.get("tool_call_id")
            name = extra.get("name") or call.get("function_name")
            args = call.get("arguments")
            rows.append(
                {
                    **base,
                    "timestamp": extra.get("started_at"),
                    "event": "tool_start",
                    "tool": {"name": name},
                    "input": args,
                    "tool_call_id": call_id,
                }
            )
            results = (step.get("observation") or {}).get("results") or [{}]
            text, meta = _tool_output(str(results[0].get("content") or ""))
            error = meta.get("status") == "error"
            rows.append(
                {
                    **base,
                    "timestamp": extra.get("ended_at"),
                    "event": "tool_error" if error else "tool_end",
                    "tool": {"name": name},
                    "tool_call_id": call_id,
                    "output": text,
                    "output_type": "error" if error else "ToolMessage",
                    "duration_seconds": extra.get("duration_seconds"),
                    **{k: v for k, v in meta.items() if v is not None},
                }
            )
        elif kind == "error":
            rows.append(
                {
                    **base,
                    "timestamp": extra.get("ended_at") or step.get("timestamp"),
                    "event": "agent_error",
                    "error_type": extra.get("error_type"),
                    "stage": extra.get("stage"),
                    "message": step.get("message"),
                }
            )
        elif kind == "final_diagnosis":
            rows.append(
                {
                    **base,
                    "timestamp": step.get("timestamp"),
                    "event": "final_diagnosis",
                    "diagnosis": extra.get("diagnosis"),
                    "text": step.get("message"),
                }
            )
        elif kind not in ("llm", "tool_call", "error", "final_diagnosis"):
            raise ValueError(f"unknown ATIF step type {kind!r}")
    error = (atif.get("extra") or {}).get("error")
    if error and not any(r["event"] == "agent_error" for r in rows):
        # Trace-level error (e.g. LangGraph recursion limit) without an error step.
        final = next((r for r in rows if r["event"] == "final_diagnosis"), None)
        row = {
            "phase": phase,
            "timestamp": (atif.get("extra") or {}).get("ended_at"),
            "event": "agent_error",
            "message": error,
            "source": "atif.extra.error",
        }
        rows.insert(rows.index(final) if final else len(rows), row)
    return rows


def _resource(location: dict[str, Any]) -> dict[str, Any]:
    device, intf = location.get("device"), location.get("interface")
    if intf:
        return {"kind": "interface", "node": device, "name": intf}
    return {"kind": "node", "node": device}


def _rc(fault_type: str, location: dict[str, Any]) -> dict[str, Any]:
    res = _resource(location)
    rid = (
        f"interface/{res['node']}/{res['name']}"
        if res["kind"] == "interface"
        else f"node/{res['node']}"
    )
    return {"fault_type": fault_type, "resource": res, "resource_id": rid}


def ground_truth(result: dict[str, Any]) -> dict[str, Any]:
    d = result["details"]
    gt = d.get("ground_truth")
    if gt is None:
        if not (d.get("negative_sample") or d.get("healthy")):
            raise ValueError(f"no ground truth for {result['trace_id']}")
        return {"is_anomaly": False, "root_causes": [], "netopsbench": None}
    out = {
        "is_anomaly": True,
        "root_causes": [_rc(gt["fault_type"], gt["location"])],
        "netopsbench": gt,
    }
    if gt.get("equivalent_locations"):
        out["equivalent_resources"] = [
            _rc(gt["fault_type"], loc)["resource_id"]
            for loc in gt["equivalent_locations"]
        ]
    return out


def submission(
    result: dict[str, Any], atif: dict[str, Any], status: str
) -> dict[str, Any]:
    final = next(
        (s for s in atif["steps"] if s["extra"].get("type") == "final_diagnosis"), None
    )
    diag = result["details"].get("agent_output")
    source = "results.agent_output"
    if diag is None:
        diag = (final or {}).get("extra", {}).get("diagnosis")
        source = "atif.final_diagnosis"
    diag = dict(diag or {})
    diag.pop("metadata", None)
    diag.pop("tool_calls", None)
    out: dict[str, Any] = {"verdict": diag.get("verdict"), "diagnosis_source": source}
    if status == "error":
        # The runtime substituted an inconclusive fallback; the agent submitted nothing.
        out["harness_fallback"] = True
        out["netopsbench_diagnosis"] = diag
        return out
    verdict = diag.get("verdict")
    if verdict == "fault_detected":
        out["is_anomaly"] = True
    elif verdict == "network_healthy":
        out["is_anomaly"] = False
    loc = diag.get("location") or {}
    out["root_causes"] = (
        [_rc(diag["fault_type"], loc)]
        if verdict == "fault_detected" and diag.get("fault_type") and loc.get("device")
        else []
    )
    report = str(diag.get("reasoning") or "")
    if diag.get("evidence"):
        report += "\n\nEvidence:\n" + "\n".join(f"- {e}" for e in diag["evidence"])
    out["diagnosis_report"] = report
    out["netopsbench_diagnosis"] = diag
    return out


def eval_metrics(result: dict[str, Any], index: dict[str, Any]) -> dict[str, Any]:
    d = result["details"]
    out: dict[str, Any] = {
        "score": result["score"],
        **{k: result[k] for k in result if k.startswith("correct_")},
    }
    for key in (
        "inconclusive",
        "false_positive",
        "interface_applicable",
        "location_match_mode",
        "matched_location",
        "fault_type_judgment",
    ):
        if key in d:
            out[key] = d[key]
    for key, src in (
        ("steps", "llm_call_count"),
        ("tool_calls", "tool_call_count"),
        ("in_tokens", "input_tokens"),
        ("out_tokens", "output_tokens"),
    ):
        if index.get(src) is not None:
            out[key] = index[src]
    return out


# ---------------------------------------------------------------- runs


def archives(raw: Path) -> list[dict[str, Any]]:
    """Manifest rows (all archives), with release and per-release fields merged."""
    rows = {r["archive_path"]: dict(r) for r in _jsonl(raw / "manifest.jsonl")}
    rel = raw / "releases" / "netopsbench-0.2" / "manifest.jsonl"
    for r in _jsonl(rel):
        rows.setdefault(r["archive_path"], {}).update(r)
    return sorted(rows.values(), key=lambda r: r["archive_path"])


def convert(raw: Path, out: Path, commits: Commits, hf_revision: str | None) -> None:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    mapping = []
    summary = []
    for man in archives(raw):
        archive = raw / man["archive_path"]
        extract = archive.with_name(archive.name.removesuffix(".tar.zst"))
        (base,) = [p for p in extract.iterdir() if p.is_dir()]
        meta = json.loads((base / "metadata.json").read_text())
        index = {r["trace_id"]: r for r in _jsonl(base / "traces" / "index.jsonl")}
        results = _jsonl(base / "traces" / "results.jsonl")
        if len(results) != len(index) or {r["trace_id"] for r in results} != set(index):
            raise ValueError(f"{archive}: results and index differ")
        release = "0.2" if man.get("benchmark_contract") else "legacy"
        starts = collections.defaultdict(list)
        for r in index.values():
            starts[r.get("source_run_id") or r["run_id"]].append(_ts(r["started_at"]))
        run_commit = {run: commits.at(min(ts)) for run, ts in starts.items()}
        first = min(t for ts in starts.values() for t in ts)
        versions = {commits.version(c) for c in run_commit.values()}
        (version,) = versions
        scale = man["scale"]
        name = "_".join(
            [
                first.strftime("%Y%m%d"),
                "nob" + version.replace(".", ""),
                "minimal_deepagent",
                _slug(man["model"]),
                _slug(scale),
            ]
        )
        run_dir = out / name
        trial_root = run_dir / "trials"
        trial_root.mkdir(parents=True)
        errors = sum(1 for r in index.values() if r["status"] == "error")
        expected = int(man["trace_error_count"])
        if errors != expected:
            raise ValueError(f"{archive}: {errors} error traces, manifest {expected}")
        commit_count = collections.Counter(
            run_commit[r.get("source_run_id") or r["run_id"]] for r in index.values()
        )
        main_commit = commit_count.most_common(1)[0][0]
        agent = {r["agent"] for r in index.values()}
        runtime = {r["runtime"] for r in index.values()}
        (agent_name,), (runtime_name,) = agent, runtime
        for result in sorted(results, key=lambda r: r["scenario_id"]):
            idx = index[result["trace_id"]]
            atif_path = base / idx["atif_path"]
            atif = json.loads(atif_path.read_text())
            source_run = idx.get("source_run_id") or idx["run_id"]
            commit = run_commit[source_run]
            trial_id = f"{result['scenario_id']}__t01"
            tdir = trial_root / trial_id
            tdir.mkdir()
            rows = messages(atif)
            with (tdir / "messages.jsonl").open("w") as fh:
                for row in rows:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            gt = ground_truth(result)
            sub = submission(result, atif, idx["status"])
            metrics = eval_metrics(result, idx)
            run = {
                "benchmark_id": "netopsbench",
                "benchmark_version": version,
                "benchmark_release": release,
                "agent_type": agent_name,
                "agent_class": idx.get("agent_class"),
                "agent_runtime": runtime_name,
                "model": idx.get("model"),
                "llm_provider": idx.get("provider"),
                "nika_git_commit": commit,
                "netopsbench_git_commit": commit,
                "commit_basis": COMMIT_BASIS,
                "scenario": scale,
                "scenario_topo_size": scale,
                # Same shape as NIKA's fingerprint; problems are the labelled fault type.
                "benchmark_fingerprint": {
                    "scenario": scale,
                    "problems": [rc["fault_type"] for rc in gt["root_causes"]],
                },
                "case_key": result["scenario_id"],
                "scenario_id": result["scenario_id"],
                "case_id": idx["case_id"],
                "episode_id": idx["episode_id"],
                "testcase_id": result.get("testcase_id"),
                "trace_id": idx["trace_id"],
                "netopsbench_run_id": idx["run_id"],
                "source_run_id": source_run,
                "snapshot_id": meta.get("snapshot_id") or meta.get("run_id"),
                "worker": idx.get("worker"),
                "difficulty": result["details"].get("difficulty"),
                "status": idx["status"],
                "error_stage": idx.get("error_stage"),
                "agent_error": (atif.get("extra") or {}).get("error"),
                "created_at": idx["started_at"],
                "start_time": idx["started_at"],
                "end_time": idx["ended_at"],
                "duration_seconds": idx.get("duration_seconds"),
                "eval_metrics": metrics,
            }
            if man.get("benchmark_contract"):
                run["benchmark_contract"] = man["benchmark_contract"]
            run = {k: v for k, v in run.items() if v is not None}
            for fname, payload in (
                ("run.json", run),
                ("submission.json", sub),
                ("ground_truth.json", gt),
                ("eval_metrics.json", metrics),
                ("netopsbench_result.json", result),
            ):
                (tdir / fname).write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
                )
            mapping.append(
                {
                    "run": name,
                    "trial_id": trial_id,
                    "archive": man["archive_path"],
                    "atif": str(atif_path.relative_to(raw)),
                    "results_jsonl": str(
                        (base / "traces" / "results.jsonl").relative_to(raw)
                    ),
                    "trace_id": idx["trace_id"],
                    "case_id": idx["case_id"],
                    "status": idx["status"],
                    "commit": commit,
                }
            )
        identity = {
            "created_at": first.isoformat(),
            "benchmark": {
                "id": "netopsbench",
                "version": version,
                "release": release,
                "contract": man.get("benchmark_contract"),
                "scale": scale,
                "case_count": len(results),
                "n_trials": 1,
                "scoring_id": "netopsbench-score",
                "leaderboard_primary": "score",
            },
            "run": {
                "run_id": meta.get("snapshot_id") or meta.get("run_id"),
                "source_runs": sorted(starts),
                "official": True,
                "agent_type": agent_name,
                "agent_runtime": runtime_name,
                "model": man["model"],
                "llm_provider": man["provider"],
                "nika_git_commit": main_commit,
                "netopsbench_git_commits": {
                    run: run_commit[run] for run in sorted(starts)
                },
                "commit_basis": COMMIT_BASIS,
                "status": man.get("status"),
                "completed_at": man.get("completed_at"),
                "trace_error_count": expected,
                "avg_score": man.get("avg_score"),
            },
            "source": {
                "hf_dataset": HF_REPO,
                "hf_revision": hf_revision,
                "archive_path": man["archive_path"],
                "archive_sha256": man.get("archive_sha256"),
                "code": GITHUB,
            },
        }
        (run_dir / "identity.yaml").write_text(
            yaml.safe_dump(identity, sort_keys=False)
        )
        metadata = {
            "info": {
                "name": f"{man['model']} minimal-deepagent ({scale}, NetOpsBench {version})",
                "authors": "NetOpsBench maintainers (NetX-lab)",
                "github": GITHUB,
            },
            "agent": {
                "model": man["model"],
                "framework": f"{agent_name} ({runtime_name})",
            },
        }
        (run_dir / "metadata.yaml").write_text(
            yaml.safe_dump(metadata, sort_keys=False)
        )
        summary.append((name, len(results), expected, sorted(set(run_commit.values()))))
    with (out / "mapping.jsonl").open("w") as fh:
        for row in mapping:
            fh.write(json.dumps(row) + "\n")
    with (out / "runs.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["run", "trials", "trace_errors", "commits"])
        for name, n, e, cs in summary:
            w.writerow([name, n, e, " ".join(c[:12] for c in cs)])
            print(name, n, e, [c[:7] for c in cs])


# ---------------------------------------------------------------- ground truth

_LEGACY_FIX_SCOPE = "nob010"  # converted-run tag of traces recorded before v0.2
_TS = re.compile(r'"(?:time|window_start_time)":"([0-9T:.\-]+Z)"')
_WINDOW = re.compile(
    r'"pingmesh_query_window": \{(?:"end_time": "[^"]+", )?"start_time": "([^"]+)"'
)


class TrialView:
    """What the ground-truth rules read from one converted trial."""

    def __init__(self, run: str, path: Path) -> None:
        self.run, self.trial_id = run, path.name
        rows = _jsonl(path / "messages.jsonl")
        self.prompt = next(r["text"] for r in rows if r["event"] == "prompt")
        self.context = next(r["text"] for r in rows if r["event"] == "initial_context")
        self.outputs = [
            (r["tool"]["name"], r.get("output") or "")
            for r in rows
            if r["event"] in ("tool_end", "tool_error")
        ]
        info = json.loads((path / "run.json").read_text())
        self.commit = info["nika_git_commit"]
        self.legacy = info["benchmark_release"] == "legacy"
        self.gt = json.loads((path / "ground_truth.json").read_text())
        self.metrics = json.loads((path / "eval_metrics.json").read_text())
        types = [rc["fault_type"] for rc in self.gt["root_causes"]]
        self.fault = types[0] if types else "healthy_network"
        self.window_start = _ts(_WINDOW.search(self.prompt).group(1))

    def output_matches(self, pattern: str, tools: set[str] | None = None) -> bool:
        rx = re.compile(pattern)
        return any(rx.search(o) for t, o in self.outputs if tools is None or t in tools)

    def stale(self, tools: set[str], slack: int = 60) -> bool:
        """A tool output carries a sample older than the episode window."""
        limit = self.window_start - dt.timedelta(seconds=slack)
        for tool, out in self.outputs:
            if tool in tools and any(_ts(m) < limit for m in _TS.findall(out)):
                return True
        return False


_EFFECT_FAULTS = {
    "packet_loss",
    "packet_corruption",
    "high_latency",
    "mtu_mismatch",
    "acl_misconfig",
    "static_route_misconfig",
    "blackhole_route",
}
_PEER_FAULTS = {"packet_loss", "packet_corruption", "high_latency", "mtu_mismatch"}


def _peer_credit(t: TrialView) -> bool:
    matched = (t.metrics.get("matched_location") or {}).get("device")
    truth = ((t.gt.get("netopsbench") or {}).get("location") or {}).get("device")
    return t.fault in _PEER_FAULTS and bool(matched) and matched != truth


# id -> (class, title, fix, status, source, rule, summary)
DEFECTS: dict[str, tuple] = {
    "NOB-ACL-INJECTED-NAME": (
        "shortcut",
        "Injected ACL named NETOPSBENCH_DENY_<device>_<prefix>",
        "30e1e72",
        "signature",
        lambda t: (
            t.fault == "acl_misconfig"
            and t.output_matches(
                r"NETOPSBENCH_DENY_|netopsbench injected deny|NETOPSBENCH_ACL:"
            )
        ),
        "The ACL handler named the injected table NETOPSBENCH_DENY_<device>_<prefix>, "
        "described it 'netopsbench injected deny <prefix>' and tagged the iptables rule "
        "NETOPSBENCH_ACL:<name>; get_device_acl, get_device_logs and get_device_config "
        "returned these, so the fault, device and prefix could be read off. 30e1e72 uses "
        "opaque random names. Trials: acl_misconfig trials whose tool outputs contain one "
        "of the three strings.",
    ),
    "NOB-EPISODE-ID-LEAK": (
        "shortcut",
        "Prompt episode id says fault or observation",
        "c845764",
        "signature",
        lambda t: re.search(
            r'"episode_id": "ep\d+_(?:fault|observation_\d+)"', t.prompt
        ),
        "Legacy generated scenarios named the diagnosis episode ep002_fault for fault "
        "cases and ep002_observation_2 for healthy cases, and the user prompt's "
        "SCENARIO_SUMMARY carried the episode id, so detection could be read from the "
        "prompt. c845764 (PR #6) uses the neutral id 'diagnosis'. Trials: prompts "
        "containing such an episode id.",
    ),
    "NOB-FALSE-MTU-ON-LOSS": (
        "harness_defect",
        "Pingmesh flags MTU/fragmentation on any loss",
        "65c21b8",
        "signature",
        lambda t: (
            t.fault != "mtu_mismatch"
            and re.search(r'"mtu_or_fragmentation_events": [1-9]', t.prompt)
        ),
        "The legacy Pingmesh detector reported mtu_or_fragmentation_suspect whenever "
        "don't-fragment probe loss rose, without checking that ordinary probes were "
        "healthy, so any loss fault also showed MTU anomalies in the prompt summary. "
        "65c21b8 requires healthy RTT probes (tightened in b9c08e2, be8c291). Trials: "
        "non-mtu_mismatch trials whose prompt reports mtu_or_fragmentation_events > 0.",
    ),
    "NOB-LOGS-NO-EPISODE-WINDOW": (
        "healthy_lure",
        "get_device_logs returns logs from before the episode",
        "c845764",
        "signature",
        lambda t: t.stale({"get_device_logs"}),
        "Legacy get_device_logs took only time_range_minutes (default 30) back from now "
        "and fell back to raw container syslog, so agents saw earlier cases' faults and "
        "recoveries and boot noise. c845764 adds start_time/end_time scoped to the "
        "episode. Trials: a get_device_logs output with a log time more than 60 s "
        "before the episode window start (pingmesh_query_window in the prompt).",
    ),
    "NOB-ROLLING-TELEMETRY-WINDOW": (
        "healthy_lure",
        "Interface metrics and default log queries use a rolling 30-minute window",
        "e234c84",
        "signature",
        lambda t: (
            t.stale({"get_interface_metrics"})
            or (not t.legacy and t.stale({"get_device_logs"}))
        ),
        "get_interface_metrics always queried range(start: -30m) without a topology "
        "filter, and v0.2 get_device_logs defaulted to the same rolling window, so "
        "counters and logs from before the episode (earlier in-place cases, health "
        "checks) entered the evidence. e234c84 scopes both to the episode window and "
        "topology. Trials: a get_interface_metrics output (or a v0.2 get_device_logs "
        "output) with a sample more than 60 s before the episode window start.",
    ),
    "NOB-SWITCH-TRACEROUTE-NO-BINARY": (
        "harness_defect",
        "traceroute from a switch fails: binary missing",
        "6b20a48",
        "signature",
        lambda t: t.output_matches(
            r"executable file not found in \$PATH", {"traceroute"}
        ),
        "The legacy traceroute tool accepted a SONiC switch as source, but SONiC "
        "containers have no traceroute binary, so the tool returned an exec error "
        "instead of a path. 6b20a48 restricts sources to clients. Trials: traceroute "
        "outputs containing 'executable file not found in $PATH'.",
    ),
    "NOB-PEER-INTERFACE-CREDIT": (
        "grading_error",
        "Peer interface credited for one-sided impairments",
        "90d1949",
        "signature",
        _peer_credit,
        "Legacy scoring treated packet_loss, packet_corruption, high_latency and "
        "mtu_mismatch as interface-symmetric, so naming the link peer earned full "
        "location credit although the handlers impaired one side only. 90d1949 limits "
        "symmetry to link_down and link_flapping. Trials: those four fault types where "
        "the scorer's matched_location device differs from the labelled device.",
    ),
    "NOB-DEVICE-DOWN-ADMIN-SHUTDOWN": (
        "label_unidentifiable",
        "device_down realized as interface shutdown on a live device",
        "aad1aa3",
        "scope",
        lambda t: t.legacy and t.fault == "device_down",
        "Legacy device_down shut every interface and stopped bgpd while the container "
        "kept answering tools, so the evidence (all ports admin down) matches an "
        "interface shutdown rather than a dead device. aad1aa3 stops the container "
        "(818900a, 0e442f7 harden the lifecycle). Trials: every legacy device_down trial.",
    ),
    "NOB-FLAP-ENDS-BEFORE-DIAGNOSIS": (
        "signal_invisible",
        "Link flapping stops before the agent looks",
        "30e1e72",
        "scope",
        lambda t: t.fault == "link_flapping",
        "link_flapping ran a fixed loop (v0.2: 6 x 2 s down / 3 s up; legacy: 10 "
        "iterations) that ended with the link up around the end of the observation "
        "window, before diagnosis, so live tools showed a healthy link. 30e1e72 keeps "
        "the flapping running until recovery. Trials: every link_flapping trial.",
    ),
    "NOB-NO-EFFECT-AUDIT": (
        "fault_not_realized",
        "Injected impairment had no observed effect",
        "30e1e72",
        "signature",
        lambda t: (
            t.fault in _EFFECT_FAULTS
            and re.search(r'"anomalies_detected": false', t.context)
        ),
        "Cases were admitted after reading back the injected state, without checking "
        "for a measured effect, so some data-plane faults produced no Pingmesh anomaly. "
        "30e1e72 adds per-fault effect audits that reject such cases. Trials: "
        "data-plane fault trials (loss, corruption, latency, MTU, ACL, static, "
        "blackhole) whose recorded observation says anomalies_detected false.",
    ),
    "NOB-CONFIG-SECTION-BROKEN": (
        "harness_defect",
        "get_device_config section filter fails silently",
        "30e1e72",
        "signature",
        lambda t: t.output_matches(
            r"% Unknown action 'section'", {"get_device_config"}
        ),
        "get_device_config(section=X) ran vtysh 'show running-config | section X', "
        "which vtysh rejects, yet the tool reported success with the error text as "
        "config. 30e1e72 filters config blocks in Python. Trials: get_device_config "
        "outputs containing \"% Unknown action 'section'\".",
    ),
    "NOB-ROUTE-POLICY-LABEL": (
        "label_unidentifiable",
        "route_policy_misconfig realized as a missing network statement",
        "30e1e72",
        "scope",
        lambda t: t.fault == "route_policy_misconfig",
        "Every route_policy_misconfig case removed a BGP network statement "
        "(misconfig_kind network_statement_missing); no route map or prefix list "
        "changed, so the label names a mechanism that is not present. 30e1e72 relabels "
        "it route_origination_misconfig. Trials: every route_policy_misconfig trial.",
    ),
    "NOB-ROUTE-PARSER-MULTIENTRY": (
        "signal_invisible",
        "get_route_table folds multiple route entries into one",
        "7ac7bfd (harness branch, not merged)",
        "signature",
        lambda t: t.output_matches(
            r'"protocol":"bgp","nexthops":\[\],"admin_distance":20,[^}]*"is_discard":true',
            {"get_route_table"},
        ),
        "The route parser merged all 'Known via' sections of 'show ip route <prefix>' "
        "into one entry, so a blackhole or static route showed as BGP with no next "
        "hops. Fixed only on the upstream harness branch (7ac7bfd). Trials: "
        "get_route_table outputs with a discard route labelled bgp/admin distance 20.",
    ),
}


def _fixed_after(git_dir: Path, fix: str, commit: str) -> bool:
    """The recorded commit does not contain the fix."""
    sha = fix.split()[0]
    return (
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(git_dir),
                "merge-base",
                "--is-ancestor",
                sha,
                commit,
            ],
            capture_output=True,
        ).returncode
        != 0
    )


def ground_truth_file(data: Path, git_dir: Path, out: Path) -> None:
    trials = [
        TrialView(run.name, path)
        for run in sorted(p for p in data.iterdir() if p.is_dir())
        for path in sorted((run / "trials").iterdir())
    ]
    result = {}
    for gid, (cls, title, fix, source, rule, summary) in DEFECTS.items():
        status = "open" if "not merged" in fix else "fixed"
        hit: dict[str, list[str]] = collections.defaultdict(list)
        for t in trials:
            if _fixed_after(git_dir, fix, t.commit) and rule(t):
                hit[t.run].append(t.trial_id)
        result[gid] = {
            "class": cls,
            "title": title,
            "summary": summary,
            "status": status,
            "source": source,
            "fix": fix,
            "trials": dict(sorted(hit.items())) or None,
        }
        n = sum(len(v) for v in hit.values())
        print(f"{gid:34} {cls:20} runs={len(hit):2} trials={n}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("convert")
    c.add_argument("--git", type=Path, required=True, help="NetOpsBench git dir")
    c.add_argument("--root", type=Path, default=ROOT)
    c.add_argument("--hf-revision", default=None)
    g = sub.add_parser("gt", help="write <root>/gt/netopsbench.json")
    g.add_argument("--git", type=Path, required=True, help="NetOpsBench git dir")
    g.add_argument("--root", type=Path, default=ROOT)
    args = ap.parse_args()
    if args.cmd == "convert":
        raw = args.root / "data" / "netopsbench-raw"
        out = args.root / "data" / "netopsbench"
        convert(raw, out, Commits(args.git), args.hf_revision)
    else:
        data = args.root / "data" / "netopsbench"
        ground_truth_file(data, args.git, args.root / "gt" / "netopsbench.json")


if __name__ == "__main__":
    main()
