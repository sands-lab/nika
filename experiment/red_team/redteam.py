"""Red Team: look for injection traces that leak a case's answer.

The red team is the evaluated agent itself: ``nika benchmark run`` with the
same cli.claude harness, MCP tools, sandbox, and submission phase. Only two
prompt strings change (diagnosis system prompt and task text, see
:func:`install_red_team_prompt`): the agent must not diagnose, only report
traces the injection left behind, with tool-call evidence and the answer each
trace implies. Its ``best_guess`` goes through the normal ``submit()`` and
scorer, which gives the shortcut success rate.

A deterministic judge then checks every trace:
  1. the excerpt occurs in the cited tool call's output (healthy_hunter's
     citation check);
  2. the inferred fault type / device matches ``ground_truth.json``;
  3. the cited calls, replayed on a fresh fault-free deployment of the same
     environment, do not reproduce the excerpt with timestamps masked
     (healthy baseline).
Excerpts that only repeat commands the agent itself ran earlier in the trial
(its own vtysh lines in ``.history_frr``) are judged ``self_inflicted``.

Phases (from the repo root; NAME is a run under ``runs/``, PHASE is
``before`` or ``after`` an injection fix):
  python experiment/red_team/redteam.py sample NAME [--types T ...] [--max-class light] [--with-healthy]
  python experiment/red_team/redteam.py run NAME [--phase before|after|whitebox] [--task-id ID ...]
  python experiment/red_team/redteam.py baseline NAME [--phase before]
  python experiment/red_team/redteam.py judge NAME
  python experiment/red_team/redteam.py oracle NAME [--phase after] [--task-id ID ...] [--batfish]
  python experiment/red_team/redteam.py recheck NAME
  python experiment/red_team/redteam.py summary NAME

Use ``.venv/bin/python`` (not ``uv run``) so a shared venv is not re-synced.
To test an injection fix kept in another worktree, prefix the ``after`` phase
(``run``/``oracle``) with ``PYTHONPATH=<worktree>/src``; spawned trial workers
inherit it, and each phase records its source tree in ``provenance.jsonl``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CONFIG = HERE / "config.yaml"
PROMPT = HERE / "prompt.md"
PROMPT_WHITEBOX = HERE / "prompt_whitebox.md"
RUNS = HERE / "runs"
CASES = REPO / "benchmark/working/cases.yaml"
# ``whitebox`` runs the auditor that is told the answer and shown the injector
# source (prompt_whitebox.md); ``before``/``after`` run the blind red team.
PHASES = ("before", "after", "whitebox")
MIN_FREE_GB = float(os.environ.get("RED_TEAM_MIN_FREE_GB", "16"))
# Set by `run` ("blind" or "whitebox") and inherited by spawned benchmark trial
# workers, which re-import this file as ``__mp_main__`` and so re-install the
# prompt patch.
RED_TEAM_ENV = "NIKA_RED_TEAM"
RED_TEAM_MODES = ("blind", "whitebox")
# Cap on the injector source pasted into a white-box task.
WHITEBOX_SOURCE_CHARS = 60000

sys.path.insert(0, str(HERE))
# A spawned trial worker (this file re-imported as ``__mp_main__``) keeps the
# effective run config `nika benchmark run` exported; the main process uses ours.
if __name__ != "__mp_main__" or "NIKA_RUN_CONFIG" not in os.environ:
    os.environ["NIKA_RUN_CONFIG"] = str(CONFIG)
import rt_trace  # noqa: E402

RED_TEAM_SYSTEM_PROMPT = """\
You are a red-team auditor of a network troubleshooting benchmark.
You look for leaked answers (injection traces), not for the fault through diagnosis.

Basic requirements:
- Use the provided MCP tools to gather information; read-only commands only.
- Never modify the network or any device state.\
"""

# The diagnosis instructions of ProblemBase.get_task_description and
# healthy_task_description; the network description around them is kept.
_DIAGNOSIS_TASK = re.compile(
    r"\n*Your goal is to analyze the network condition.*?remediation steps at this stage\.",
    re.DOTALL,
)


# ------------------------------------------------------------- prompt patch


def fault_type_ids() -> list[dict[str, str]]:
    from nika.problems.ownership import ownership_entries
    from nika.workflows.agent.submission import fault_candidates

    return ownership_entries(fault_candidates())


def red_team_task(task_description: str, whitebox: str | None = None) -> str:
    """Red-team instructions plus the network description the agent would get.

    With ``whitebox`` (ground truth and injector source), the white-box prompt
    is used and that context is appended.
    """
    if not _DIAGNOSIS_TASK.search(task_description):
        raise RuntimeError("task description no longer matches the diagnosis template")
    network = _DIAGNOSIS_TASK.sub("", task_description).strip()
    network = network.removeprefix(
        "You are provided with the following network description and its current state:"
    ).strip()
    types = "\n".join(f"- `{e['id']}`: {e['description']}" for e in fault_type_ids())
    prompt = PROMPT_WHITEBOX if whitebox is not None else PROMPT
    instructions = prompt.read_text(encoding="utf-8").replace("{fault_types}", types)
    task = f"{instructions.rstrip()}\n\n## Network under test\n\n{network}"
    if whitebox is not None:
        task += f"\n\n{whitebox}"
    return task


def injector_source(problem_names: list[str]) -> str:
    """Source of each injected problem class (and its NIKA problem bases), plus
    the module-level NIKA helpers those classes call, one level deep."""
    import inspect

    from nika.problems.registry import get_problem_class

    seen: set[object] = set()
    parts: list[str] = []

    def add(obj: object) -> None:
        if obj in seen:
            return
        seen.add(obj)
        try:
            source = inspect.getsource(obj)  # type: ignore[arg-type]
        except (OSError, TypeError):
            return
        parts.append(f"# {obj.__module__}.{obj.__qualname__}\n{source}")  # type: ignore[attr-defined]

    classes = []
    for name in problem_names:
        cls = get_problem_class(name)
        if cls is None:
            continue
        for base in cls.__mro__:
            if (
                base.__module__.startswith("nika.problems")
                and base.__name__ != "ProblemBase"
            ):
                classes.append(base)
                add(base)
    for cls in classes:
        scope = vars(sys.modules[cls.__module__])
        for ref in re.findall(r"\b([A-Za-z_]\w*)\(", inspect.getsource(cls)):
            obj = scope.get(ref)
            if (
                (inspect.isfunction(obj) or inspect.isclass(obj))
                and getattr(obj, "__module__", "").startswith("nika.")
                and obj.__module__ != "nika.problems.base"
            ):
                add(obj)
    text = "\n\n".join(parts)
    if len(text) > WHITEBOX_SOURCE_CHARS:
        text = text[:WHITEBOX_SOURCE_CHARS] + "\n# ... (truncated)"
    return text


def whitebox_context(session: Any) -> str:
    """Ground truth and injector source for the white-box auditor."""
    gt = _read_json(Path(session.session_dir) / "ground_truth.json") or {}
    problems = list(getattr(session, "problem_names", None) or [])
    return (
        "## Ground truth (known to you only)\n\n"
        f"```json\n{json.dumps(gt, indent=2)}\n```\n\n"
        "## Injector source\n\n"
        "Runtime commands run as `sh -c` inside the device container "
        "(`self.runtime.exec(node, cmd)`).\n\n"
        f"```python\n{injector_source(problems)}\n```"
    )


def install_red_team_prompt(mode: str = "blind") -> None:
    """Swap the diagnosis prompt strings; the rest of the benchmark is unchanged."""
    import agent.cli.claude.phases.diagnosis as diagnosis
    import nika.workflows.benchmark.run as bench_run
    from nika.utils.session import Session

    diagnosis.OVERALL_DIAGNOSIS_PROMPT = RED_TEAM_SYSTEM_PROMPT
    original = bench_run.start_agent
    if getattr(original, "red_team", False):
        return

    def start_agent(*args: Any, session_id: str | None = None, **kwargs: Any) -> None:
        session = Session().load_running_session(session_id=session_id)
        extra = whitebox_context(session) if mode == "whitebox" else None
        session.update_session(
            "task_description", red_team_task(session.task_description, extra)
        )
        return original(*args, session_id=session_id, **kwargs)

    start_agent.red_team = True  # type: ignore[attr-defined]
    bench_run.start_agent = start_agent


# "1" is the blind mode's value in runs started before the white-box mode.
_mode = {"1": "blind"}.get(
    os.environ.get(RED_TEAM_ENV, ""), os.environ.get(RED_TEAM_ENV)
)
if _mode in RED_TEAM_MODES:
    install_red_team_prompt(_mode)


# ------------------------------------------------------------------ helpers


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


# Timestamps differ between deployments (ls -l, stat, ps lstart, daemon logs).
_TIMESTAMP = re.compile(
    r"\d{4}[-/]\d{2}[-/]\d{2}[ t]\d{2}:\d{2}(:\d{2}(\.\d+)?)?( [+-]\d{4})?"
    r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec) +\d{1,2} +(\d{2}:\d{2}(:\d{2})?|\d{4})\b"
)


def excerpt_in(excerpt: Any, output: Any, *, across_deployments: bool = False) -> bool:
    """Excerpt occurs in output, verbatim or as a set of whole lines.

    Models often reorder or drop repeated lines when quoting multi-line output
    (history files, ps); each non-empty excerpt line must still appear. When
    the output comes from another deployment, timestamps are masked first.
    """

    def norm(text: Any) -> str:
        text = _norm(text)
        return _TIMESTAMP.sub("<time>", text) if across_deployments else text

    text = norm(output)
    if not norm(excerpt) or not text:
        return False
    if norm(excerpt) in text:
        return True
    lines = {norm(line) for line in str(excerpt).splitlines() if norm(line)}
    return len(lines) > 1 and all(line in text for line in lines)


def self_inflicted(item: dict, calls: list[dict], matched_call_id: str | None) -> bool:
    """Every excerpt line was a command the agent itself ran before the cited
    call, e.g. its own vtysh commands quoted back from ``.history_frr``."""
    ids = [c["id"] for c in calls]
    if matched_call_id not in ids:
        return False
    earlier = " ".join(
        _norm(" ".join(str(v) for v in c["args"].values()))
        for c in calls[: ids.index(matched_call_id)]
    )
    lines = {_norm(line) for line in str(item.get("excerpt") or "").splitlines()}
    lines.discard("")
    return bool(lines) and all(line in earlier for line in lines)


def check_evidence(item: dict, calls: list[dict]) -> dict[str, Any]:
    """Citation check (from healthy_hunter), accepting line-set excerpts."""
    check = rt_trace.check_evidence(item, calls)
    if check["status"] != "verified":
        tool = str(item.get("tool") or "").split("__")[-1]
        args = json.dumps(item.get("args") or {}, sort_keys=True)
        for call in calls:
            if (
                call["tool"] == tool
                and json.dumps(call["args"], sort_keys=True) == args
                and excerpt_in(item.get("excerpt"), call["output"])
            ):
                return {**check, "status": "verified", "matched_call_id": call["id"]}
    return check


def run_dir(name: str) -> Path:
    return RUNS / name


def load_rows(name: str) -> list[dict[str, Any]]:
    return yaml.safe_load((run_dir(name) / "cases.yaml").read_text())["cases"]


def rows_by_case_key(name: str) -> dict[str, dict[str, Any]]:
    from nika.workflows.benchmark.load_config import normalize_benchmark_row
    from nika.workflows.benchmark.trials import case_key_for_row

    return {case_key_for_row(normalize_benchmark_row(r)): r for r in load_rows(name)}


def env_key(row: dict[str, Any]) -> str:
    """Deployment identity: rows with the same key share one healthy baseline."""
    parts = [row["scenario"]]
    for key in (
        "topo_size",
        "topo",
        "igp",
        "bgp_mode",
        "rpki",
        "backend",
        "device_profile",
    ):
        if row.get(key) not in (None, "", False):
            parts.append(f"{key}={row[key]}")
    return "__".join(str(p) for p in parts)


def trial_dirs(name: str, phase: str) -> list[Path]:
    root = run_dir(name) / phase / "trials"
    return sorted(p for p in root.glob("*") if p.is_dir()) if root.is_dir() else []


def preflight(*, need_model: bool = True) -> None:
    """Refuse to start while the host is short of memory or the model is down."""
    import urllib.request

    free = rt_trace.mem_available_gb()
    if free < MIN_FREE_GB:
        raise SystemExit(
            f"only {free:.1f} GiB available (< {MIN_FREE_GB}); not starting"
        )
    if not need_model:
        return
    agent = yaml.safe_load(CONFIG.read_text())["agent"]
    url = agent["custom"]["base_url"].rstrip("/") + "/models"
    with urllib.request.urlopen(url, timeout=10) as resp:
        models = [m.get("id") for m in json.loads(resp.read()).get("data") or []]
    if agent["model"] not in models:
        raise SystemExit(f"{url} does not serve {agent['model']}")


def record_provenance(result_dir: Path) -> None:
    """Which NIKA source tree (``PYTHONPATH`` may point at a fix worktree),
    commit, and diff a phase ran with, plus the prompt and config used."""
    import hashlib
    import subprocess

    import nika

    src = Path(nika.__file__).resolve().parents[2]

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=src, capture_output=True, text=True, check=False
        ).stdout

    entry = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "nika_src": str(src),
        "git_commit": git("rev-parse", "HEAD").strip(),
        "git_branch": git("rev-parse", "--abbrev-ref", "HEAD").strip(),
        "src_diff_sha256": hashlib.sha256(
            git("diff", "HEAD", "--", "src").encode()
        ).hexdigest(),
        "src_diff_files": git("diff", "--name-only", "HEAD", "--", "src").split(),
        "prompt_sha256": hashlib.sha256(PROMPT.read_bytes()).hexdigest(),
        "prompt_whitebox_sha256": hashlib.sha256(
            PROMPT_WHITEBOX.read_bytes()
        ).hexdigest(),
        "config_sha256": hashlib.sha256(CONFIG.read_bytes()).hexdigest(),
    }
    result_dir.mkdir(parents=True, exist_ok=True)
    with (result_dir / "provenance.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    print(
        f"[provenance] nika from {src} @ {entry['git_branch']} {entry['src_diff_files']}"
    )


def nika_cli(argv: list[str]) -> None:
    from nika.cli.main import main

    result_dir = Path(argv[argv.index("--result_dir") + 1])
    record_provenance(result_dir)
    sys.argv = ["nika", *argv]
    try:
        main()
    except SystemExit as exc:
        if exc.code not in (0, None):
            raise


# ------------------------------------------------------------------- sample

_CLASS_RANK = {"light": 0, "large": 1, "k8s": 2, "clab": 3}
_SIZE_RANK = {"s": 0, "": 1, "m": 2, "l": 3}


def sample(
    name: str, types: list[str] | None, max_class: str, with_healthy: bool
) -> None:
    """One case per fault type (the cheapest to deploy), optionally one healthy per env."""
    from nika.workflows.benchmark.admit import resource_class_for_row

    cases = yaml.safe_load(CASES.read_text())["cases"]
    by_type: dict[str, list[dict]] = defaultdict(list)
    for case in cases:
        by_type[case["problem"]].append(case)
    limit = _CLASS_RANK[max_class]

    def cost(case: dict) -> tuple:
        cls = resource_class_for_row(case)
        return (_CLASS_RANK[cls], _SIZE_RANK.get(str(case.get("topo_size") or ""), 1))

    picked, skipped = [], []
    for problem in sorted(by_type):
        if problem == "healthy" or (types and problem not in types):
            continue
        best = min(by_type[problem], key=cost)
        (picked if cost(best)[0] <= limit else skipped).append(best)
    if with_healthy:
        envs = {env_key(c) for c in picked}
        healthy = {env_key(c): c for c in by_type.get("healthy", [])}
        picked += [healthy[k] for k in sorted(envs) if k in healthy]
    out = run_dir(name) / "cases.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump({"cases": picked}, sort_keys=False))
    print(f"{len(picked)} case(s) -> {out.relative_to(REPO)}")
    if skipped:
        print(
            f"skipped (class above {max_class}): {sorted(c['problem'] for c in skipped)}"
        )


# ---------------------------------------------------------------- run/oracle


def trial_failure(trial: Path) -> str | None:
    """Why a trial carries no red-team signal, or None.

    ``endpoint``: the agent failed without a single tool call (the model
    endpoint errored; benchmark resume keeps ``agent_failed`` trials).
    ``tools_failed``: every MCP tool call returned an error, so no device was
    observed (e.g. issue #123).
    """
    calls = rt_trace.load_tool_calls(trial / "messages.jsonl")
    outcome = (_read_json(trial / "run.json") or {}).get("outcome")
    if not calls and outcome == "agent_failed":
        return "endpoint"
    # Diagnosis-phase device tools; the submission phase only has ``submit``.
    mcp = [c for c in calls if c["name"].startswith("mcp__") and c["tool"] != "submit"]
    if mcp and all(c["is_error"] for c in mcp):
        return "tools_failed"
    return None


def quarantine_failed_trials(name: str, phase: str) -> None:
    """Move trials without signal aside so benchmark resume runs them again.

    A trial is moved at most twice (``<phase>-quarantine/<trial>.<n>``); after
    that it stays and the summary lists it under ``invalid_trials``.
    """
    aside = run_dir(name) / f"{phase}-quarantine"
    for trial in trial_dirs(name, phase):
        reason = trial_failure(trial)
        if reason is None:
            continue
        tries = len(list(aside.glob(f"{trial.name}.*"))) if aside.is_dir() else 0
        if tries >= 2:
            print(f"[quarantine] {trial.name}: {reason} again, kept")
            continue
        aside.mkdir(parents=True, exist_ok=True)
        trial.rename(aside / f"{trial.name}.{tries + 1}")
        print(f"[quarantine] {trial.name}: {reason}, will run again")


def run(
    name: str, phase: str, task_ids: list[str], batch_size: int | None = None
) -> None:
    preflight()
    mode = "whitebox" if phase == "whitebox" else "blind"
    os.environ[RED_TEAM_ENV] = mode
    install_red_team_prompt(mode)
    quarantine_failed_trials(name, phase)
    cases = run_dir(name) / "cases.yaml"
    if mode == "whitebox":
        # A healthy case has nothing injected to audit.
        rows = [r for r in load_rows(name) if r["problem"] != "healthy"]
        cases = run_dir(name) / "whitebox-cases.yaml"
        cases.write_text(yaml.safe_dump({"cases": rows}, sort_keys=False))
    argv = [
        "benchmark",
        "run",
        "--config",
        str(cases),
        "--run-config",
        str(CONFIG),
        "--result_dir",
        str(run_dir(name) / phase),
        "--output-mode",
        "agent",
        "-y",
    ]
    for task_id in task_ids:
        argv += ["--task-id", task_id]
    if batch_size:
        # Concurrent trials; serialize_heavy still runs heavy labs alone.
        argv += ["--batch-size", str(batch_size)]
    nika_cli(argv)


def oracle(
    name: str,
    phase: str,
    task_ids: list[str],
    batfish: bool = False,
    batch_size: int | None = None,
) -> None:
    """Mock (ground-truth) agent with failure-effect validation on: the fault
    must still inject, verify, and score after an injection change."""
    os.environ.pop(RED_TEAM_ENV, None)
    preflight(need_model=False)
    config = yaml.safe_load(CONFIG.read_text())
    config["agent"] = {"type": "mock", "max_steps": 5, "timeout_sec": 600}
    config["nika"]["runtime_validation"]["failure_effect"] = True
    # ISP effect contracts compare healthy/faulty Batfish reports too.
    config["nika"]["static_validation"]["enabled"] = batfish
    derived = run_dir(name) / f"oracle-{phase}.config.yaml"
    derived.write_text(yaml.safe_dump(config, sort_keys=False))
    rows = [r for r in load_rows(name) if r["problem"] != "healthy"]
    cases = run_dir(name) / "oracle-cases.yaml"
    cases.write_text(yaml.safe_dump({"cases": rows}, sort_keys=False))
    argv = [
        "benchmark",
        "run",
        "--config",
        str(cases),
        "--run-config",
        str(derived),
        "--result_dir",
        str(run_dir(name) / f"oracle-{phase}"),
        "--output-mode",
        "agent",
        "-y",
    ]
    for task_id in task_ids:
        argv += ["--task-id", task_id]
    if batch_size:
        # Concurrent trials; serialize_heavy still runs heavy labs alone.
        argv += ["--batch-size", str(batch_size)]
    nika_cli(argv)


# -------------------------------------------------------------------- judge


def report_json(name: str, phase: str, trial: Path) -> tuple[dict | None, bool]:
    """The trial's red-team report JSON and whether it was finalized offline.

    A trial that used its whole turn budget without a final text has no
    submission; as in healthy_hunter, ask the same model once, without tools,
    to write the report from the recorded tool calls (cached under
    ``runs/NAME/finalized/``).
    """
    submission = _read_json(trial / "submission.json") or {}
    parsed = rt_trace.extract_report_json(submission.get("diagnosis_report") or "")
    if parsed is not None:
        return parsed, False
    cached = run_dir(name) / "finalized" / phase / f"{trial.name}.md"
    if not cached.is_file():
        messages = trial / "messages.jsonl"
        if not rt_trace.max_steps_reached(messages):
            return None, False
        task = (_read_json(trial / "run.json") or {}).get("task_description") or ""
        prompt = rt_trace.finalize_prompt(task, rt_trace.load_tool_calls(messages))
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(rt_trace.finalize_report(prompt, CONFIG), encoding="utf-8")
    return rt_trace.extract_report_json(cached.read_text(encoding="utf-8")), True


def gt_targets(gt: dict) -> tuple[set[str], set[str]]:
    """Ground-truth fault types and the device names their resources touch."""
    types, devices = set(), set()
    for cause in gt.get("root_causes") or []:
        types.add(str(cause.get("fault_type")))
        resource = cause.get("resource") or {}
        if resource.get("node"):
            devices.add(str(resource["node"]))
        if resource.get("name"):
            devices.add(str(resource["name"]))
        rid = str(cause.get("resource_id") or resource.get("id") or "")
        kind, _, rest = rid.partition("/")
        if kind == "link":
            devices.update(end.split(":")[0] for end in rest.split("--"))
        elif kind == "interface":
            devices.add(rest.split("/")[0])
    return types, devices


def answer_match(inferred: dict | None, gt: dict) -> dict[str, bool]:
    from nika.problems.registry import resolve_problem_name

    inferred = inferred or {}
    types, devices = gt_targets(gt)
    fault_type = inferred.get("fault_type")
    try:
        fault_type = resolve_problem_name(str(fault_type)) if fault_type else None
    except Exception:  # noqa: BLE001 - an unknown id is simply wrong
        pass
    return {
        "fault_type": bool(fault_type) and fault_type in types,
        "device": bool(inferred.get("device")) and str(inferred["device"]) in devices,
    }


def targeted_values(row: dict) -> list[str]:
    """Inject parameter values only someone who knows the answer would type:
    prefixes, IPs, communities, rule and file names. Node and interface names
    are left out (a blind sweep covers every device and interface), and so are
    plain words, which are too common to tell anything."""
    values = []
    for key, value in (row.get("inject") or {}).items():
        if re.search(
            r"host|device|node|router|switch|peer|observer|intf|iface|interface", key
        ):
            continue
        text = str(value).strip().lower()
        if len(text) >= 4 and re.search(r"[\d./:_-]", text) and not text.isdigit():
            values.append(text)
    return values


def targeted(trace: dict, values: list[str]) -> bool:
    """A white-box trace whose evidence command uses an answer-specific value,
    so an agent that does not know the answer would not have run it."""
    commands = " ".join(
        str(v).lower()
        for e in trace["evidence"]
        for v in (e.get("args") or {}).values()
    )
    return any(value in commands for value in values)


def trace_verdict(trace: dict) -> str:
    if trace["citation"] != "verified":
        return "unsupported"
    if trace["self_inflicted"]:
        return "self_inflicted"
    if trace.get("targeted"):
        return "targeted"
    match = trace["answer_match"]
    if not (match["fault_type"] or match["device"]):
        return "wrong_answer"
    baseline = trace["baseline"]
    if baseline == "present":
        return "in_baseline"
    if baseline != "absent":
        return "baseline_pending"
    return "confirmed"


def baseline_status(trace: dict, replay: dict | None) -> str:
    """``absent`` when no replayed evidence call reproduces its excerpt."""
    if replay is None:
        return "not_replayed"
    results = [
        c
        for c in replay.get("calls") or []
        if c["trace_key"] == trace["trace_key"] and "skipped" not in c
    ]
    if not results:
        return "not_replayed"
    if any(replay_reproduces(c) for c in results):
        return "present"
    if all(replay_failed(c) for c in results):
        return "replay_error"
    return "absent"


def replay_reproduces(call: dict) -> bool:
    return excerpt_in(call.get("excerpt"), call.get("output"), across_deployments=True)


def replay_failed(call: dict) -> bool:
    """No output, or the command hit the exec deadline (e.g. under CPU stress)."""
    output = call.get("output")
    return output is None or "[TIMEOUT]" in str(output)


def recheck_status(record: dict, trace_key: str) -> str:
    calls = [c for c in record.get("calls") or [] if c["trace_key"] == trace_key]
    if record.get("status") != "ok":
        return "error"
    if any(replay_reproduces(c) for c in calls):
        return "still_present"
    if not calls or all(replay_failed(c) or "skipped" in c for c in calls):
        return "inconclusive"
    return "gone"


def judge(name: str) -> list[dict]:
    keyed = rows_by_case_key(name)
    rows = []
    for phase in PHASES:
        for trial in trial_dirs(name, phase):
            meta = _read_json(trial / "run.json") or {}
            row = keyed.get(meta.get("case_key")) or {}
            gt = _read_json(trial / "ground_truth.json") or {}
            report, finalized = report_json(name, phase, trial)
            calls = rt_trace.load_tool_calls(trial / "messages.jsonl")
            replay = _read_json(
                run_dir(name) / "baseline" / phase / f"{trial.name}.json"
            )
            for idx, trace in enumerate((report or {}).get("traces") or [], start=1):
                checks = [check_evidence(e, calls) for e in trace.get("evidence") or []]
                item = {
                    "trace_key": f"{trial.name}/{trace.get('id') or f'T{idx}'}",
                    "phase": phase,
                    "trial": trial.name,
                    "finalized": finalized,
                    "problem": row.get("problem")
                    or ",".join(meta.get("problem_names") or []),
                    "env_key": env_key(row) if row else meta.get("scenario_name"),
                    "trace_type": trace.get("trace_type") or "other",
                    "part_of_fault_mechanism": trace.get("part_of_fault_mechanism"),
                    "device": trace.get("device"),
                    "observation": trace.get("observation"),
                    "inferred_answer": trace.get("inferred_answer"),
                    "evidence": trace.get("evidence") or [],
                    "citation": rt_trace.citation_status(checks),
                    "evidence_checks": checks,
                    "self_inflicted": bool(checks)
                    and all(
                        self_inflicted(e, calls, c["matched_call_id"])
                        for e, c in zip(trace.get("evidence") or [], checks)
                    ),
                    "answer_match": answer_match(trace.get("inferred_answer"), gt),
                    "trial_failure": trial_failure(trial),
                }
                if phase == "whitebox":
                    item["targeted"] = targeted(item, targeted_values(row))
                item["baseline"] = baseline_status(item, replay)
                item["verdict"] = trace_verdict(item)
                rows.append(item)
    out = run_dir(name) / "traces.jsonl"
    out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"{len(rows)} trace(s) -> {out.relative_to(REPO)}")
    print(dict(Counter(r["verdict"] for r in rows)))
    return rows


# ----------------------------------------------------------------- baseline


def evidence_calls(traces: list[dict]) -> list[dict]:
    """The traces' cited calls, with mutating commands marked as skipped."""
    wanted = []
    for trace in traces:
        for idx, item in enumerate(trace["evidence"]):
            call = {
                "trace_key": trace["trace_key"],
                "trial": trace["trial"],
                "evidence_index": idx,
                "tool": str(item.get("tool") or "").split("__")[-1],
                "args": item.get("args") or {},
                "excerpt": item.get("excerpt"),
            }
            if rt_trace.mutating_calls(
                [{"id": "x", "tool": call["tool"], "args": call["args"]}]
            ):
                call["skipped"] = "mutating command"
            wanted.append(call)
    return wanted


def deploy_and_replay(
    row: dict, wanted: list[dict], sessions: Path, *, inject: bool
) -> dict[str, Any]:
    """Deploy the row's environment (and inject its fault when ``inject``),
    replay ``wanted`` through the session's MCP tools, then close the session."""
    from agent.utils.mcp_client import load_session_mcp_config
    from nika.mcp.gateway.lifecycle import mcp_gateway_for_session
    from nika.net_env.net_env_pool import scenario_requires_topo_size
    from nika.utils.agent_session_id import resolve_agent_session_id
    from nika.utils.session import Session
    from nika.utils.session_id import make_session_id
    from nika.workflows.benchmark.multi_fault import row_problems
    from nika.workflows.env.start import start_net_env
    from nika.workflows.failure.inject import inject_failure
    from nika.workflows.session.close import close_session

    session_id = make_session_id(session_tag="rtcheck" if inject else "rtbase")
    record: dict[str, Any] = {"env_key": env_key(row), "session_id": session_id}
    deployed = False
    started = time.time()
    try:
        size = (
            row.get("topo_size") or None
            if scenario_requires_topo_size(row["scenario"])
            else None
        )
        start_net_env(
            row["scenario"],
            size,
            session_id=session_id,
            result_dir=str(sessions),
            **{
                k: row[k]
                for k in (
                    "topo",
                    "igp",
                    "bgp_mode",
                    "rpki",
                    "backend",
                    "device_profile",
                )
                if row.get(k) is not None
            },
        )
        deployed = True
        if inject:
            inject_failure(
                problem_names=row_problems(row),
                session_id=session_id,
                param_overrides=dict(row.get("inject") or {}),
                expected_root_causes=row.get("root_causes"),
            )
        session = Session().load_running_session(session_id=session_id)
        with mcp_gateway_for_session(
            session_id,
            scenario_name=session.scenario_name,
            backend=getattr(session, "backend", None),
        ):
            servers = load_session_mcp_config(
                resolve_agent_session_id(session),
                session.scenario_name,
                backend=getattr(session, "backend", None),
                phase="diagnosis",
            )
            todo = [w for w in wanted if "skipped" not in w]
            results = asyncio.run(rt_trace.call_tools(servers, todo))
        record["calls"] = results + [w for w in wanted if "skipped" in w]
        record["status"] = "ok"
    except Exception as exc:  # noqa: BLE001 - recorded, judged as replay_error
        record["status"] = "error"
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["calls"] = []
    finally:
        if deployed:
            close_session(session_id=session_id, undeploy=True, status="finished")
        record["duration_sec"] = round(time.time() - started, 1)
    return record


def _trial_rows(name: str, phase: str) -> dict[str, dict]:
    keyed = rows_by_case_key(name)
    rows = {}
    for trial in trial_dirs(name, phase):
        meta = _read_json(trial / "run.json") or {}
        if meta.get("case_key") in keyed:
            rows[trial.name] = keyed[meta["case_key"]]
    return rows


def baseline(name: str, phase: str) -> None:
    """Replay each trial's cited evidence calls on a fresh healthy deployment.

    One deployment per environment; outputs go to
    ``runs/NAME/baseline/PHASE/<trial>.json``.
    """
    preflight()
    traces = [
        t for t in judge(name) if t["phase"] == phase and t["citation"] != "fabricated"
    ]
    trial_rows = _trial_rows(name, phase)
    by_env: dict[str, list[dict]] = defaultdict(list)
    for trace in traces:
        if trace["trial"] in trial_rows:
            by_env[trace["env_key"]].append(trace)

    out_dir = run_dir(name) / "baseline" / phase
    for key, env_traces in by_env.items():
        replayed = {
            c["trace_key"]
            for t in env_traces
            for d in [_read_json(out_dir / f"{t['trial']}.json")]
            if d and d.get("status") == "ok"
            for c in d["calls"]
        }
        if all(t["trace_key"] in replayed for t in env_traces if t["evidence"]):
            print(f"[baseline] {key}: already replayed")
            continue
        wanted = evidence_calls(env_traces)
        record = deploy_and_replay(
            trial_rows[env_traces[0]["trial"]],
            wanted,
            out_dir / "sessions",
            inject=False,
        )
        for trial in {t["trial"] for t in env_traces}:
            part = {
                **record,
                "calls": [c for c in record["calls"] if c["trial"] == trial],
            }
            _write_json(out_dir / f"{trial}.json", part)
        print(f"[baseline] {key}: {record['status']} ({len(wanted)} call(s))")
    judge(name)


def recheck(name: str) -> None:
    """After an injection fix: re-inject each case with a confirmed ``before``
    trace (with the NIKA source on ``PYTHONPATH``) and replay the trace's
    evidence calls; the fix holds when no excerpt reproduces.

    Outputs go to ``runs/NAME/recheck/<trial>.json``.
    """
    preflight(need_model=False)
    confirmed = [
        t for t in judge(name) if t["phase"] == "before" and t["verdict"] == "confirmed"
    ]
    trial_rows = _trial_rows(name, "before")
    out_dir = run_dir(name) / "recheck"
    record_provenance(out_dir)
    by_trial: dict[str, list[dict]] = defaultdict(list)
    for trace in confirmed:
        by_trial[trace["trial"]].append(trace)
    for trial, traces in by_trial.items():
        record = deploy_and_replay(
            trial_rows[trial], evidence_calls(traces), out_dir / "sessions", inject=True
        )
        record["traces"] = {
            t["trace_key"]: recheck_status(record, t["trace_key"]) for t in traces
        }
        _write_json(out_dir / f"{trial}.json", record)
        print(f"[recheck] {trial}: {record['status']} {record['traces']}")


# ------------------------------------------------------------------ summary


def _phase_cases(name: str, phase: str, traces: list[dict]) -> dict[str, Any]:
    trials = trial_dirs(name, phase)
    faulty = [
        t
        for t in trials
        if (_read_json(t / "ground_truth.json") or {}).get("is_anomaly") is not False
    ]
    healthy = [t for t in trials if t not in faulty]
    confirmed = [
        t for t in traces if t["phase"] == phase and t["verdict"] == "confirmed"
    ]
    with_trace = {t["trial"] for t in confirmed}
    full = {
        t["trial"]
        for t in confirmed
        if t["answer_match"]["fault_type"] and t["answer_match"]["device"]
    }
    scored = []
    for trial in faulty:
        metrics = _read_json(trial / "eval_metrics.json")
        if metrics is not None:
            scored.append(metrics)
    healthy_fp = [
        t.name
        for t in healthy
        if ((_read_json(t / "submission.json") or {}).get("is_anomaly")) is True
    ]
    # best_guess of the (possibly finalized) report, scored like a trace: a
    # trial whose turn budget ran out has no benchmark submission.
    guesses = []
    for trial in faulty:
        report, _ = report_json(name, phase, trial)
        gt = _read_json(trial / "ground_truth.json") or {}
        guesses.append(answer_match((report or {}).get("best_guess"), gt))
    return {
        "faulty_cases": len(faulty),
        "healthy_cases": len(healthy),
        "reports_parsed": sum(
            1 for t in trials if report_json(name, phase, t)[0] is not None
        ),
        "cases_with_confirmed_trace": len(with_trace & {t.name for t in faulty}),
        "cases_with_full_answer_trace": len(full & {t.name for t in faulty}),
        "shortcut_rca_success": sum(1 for m in scored if m.get("rca_accuracy") == 1.0),
        "shortcut_localization_success": sum(
            1 for m in scored if m.get("localization_accuracy") == 1.0
        ),
        "shortcut_fault_type_success": sum(
            1 for m in scored if m.get("fault_type_recall") == 1.0
        ),
        "scored_cases": len(scored),
        "best_guess_full": sum(1 for g in guesses if g["fault_type"] and g["device"]),
        "best_guess_fault_type": sum(1 for g in guesses if g["fault_type"]),
        "best_guess_device": sum(1 for g in guesses if g["device"]),
        "healthy_false_positive": healthy_fp,
        "invalid_trials": {
            t.name: reason for t in trials if (reason := trial_failure(t)) is not None
        },
    }


def _oracle_cases(name: str, phase: str) -> dict[str, Any] | None:
    root = run_dir(name) / f"oracle-{phase}" / "trials"
    if not root.is_dir():
        return None
    rows = []
    for trial in sorted(p for p in root.glob("*") if p.is_dir()):
        metrics = _read_json(trial / "eval_metrics.json") or {}
        effect = _read_json(trial / "validation-failure-effect.json") or {}
        meta = _read_json(trial / "run.json") or {}
        rows.append(
            {
                "trial": trial.name,
                "outcome": meta.get("outcome"),
                "rca_accuracy": metrics.get("rca_accuracy"),
                "failure_effect": effect.get("status", "no_contract"),
                "passed": meta.get("outcome") == "success"
                and metrics.get("rca_accuracy") == 1.0
                and effect.get("status", "PASS") in {"PASS", "UNSUPPORTED"},
            }
        )
    return {"cases": len(rows), "passed": sum(r["passed"] for r in rows), "rows": rows}


def summary(name: str) -> None:
    traces = judge(name)
    phases = {p: _phase_cases(name, p, traces) for p in PHASES if trial_dirs(name, p)}
    by_type: dict[str, Counter] = defaultdict(Counter)
    for t in traces:
        by_type[t["trace_type"]][f"{t['phase']}:{t['verdict']}"] += 1
    oracle_rows = {p: _oracle_cases(name, p) for p in PHASES}
    data = {
        "phases": phases,
        "trace_verdicts": dict(Counter(f"{t['phase']}:{t['verdict']}" for t in traces)),
        "by_trace_type": {k: dict(v) for k, v in sorted(by_type.items())},
        "side_channel_confirmed": sum(
            1
            for t in traces
            if t["verdict"] == "confirmed" and t["part_of_fault_mechanism"] is False
        ),
        "oracle": oracle_rows,
    }
    _write_json(run_dir(name) / "summary.json", data)

    lines = [f"# Red Team summary: {name}", ""]
    lines += [
        "| Metric | " + " | ".join(phases) + " |",
        "|---|" + "---:|" * len(phases),
    ]
    for key in (
        "faulty_cases",
        "healthy_cases",
        "reports_parsed",
        "cases_with_confirmed_trace",
        "cases_with_full_answer_trace",
        "scored_cases",
        "shortcut_rca_success",
        "shortcut_localization_success",
        "shortcut_fault_type_success",
        "best_guess_full",
        "best_guess_fault_type",
        "best_guess_device",
    ):
        lines.append(
            f"| {key} | " + " | ".join(str(v[key]) for v in phases.values()) + " |"
        )
    lines.append(
        "| healthy_false_positive | "
        + " | ".join(str(len(v["healthy_false_positive"])) for v in phases.values())
        + " |"
    )
    lines.append(
        "| invalid_trials | "
        + " | ".join(str(len(v["invalid_trials"])) for v in phases.values())
        + " |"
    )
    invalid = [
        (phase, trial, reason)
        for phase, v in phases.items()
        for trial, reason in v["invalid_trials"].items()
    ]
    if invalid:
        lines += ["", "## Invalid trials (no device observed)", ""]
        lines += [f"- {phase} {trial}: {reason}" for phase, trial, reason in invalid]
    lines += ["", "## Traces by type", "", "| Trace type | Verdicts |", "|---|---|"]
    for k, v in sorted(by_type.items()):
        lines.append(
            f"| {k} | " + ", ".join(f"{a}={b}" for a, b in sorted(v.items())) + " |"
        )
    lines += [
        "",
        "## Traces",
        "",
        "| Phase | Trial | Type | Mechanism | Inferred | Citation | Baseline | Verdict |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for t in traces:
        inferred = t["inferred_answer"] or {}
        lines.append(
            f"| {t['phase']} | {t['trial']} | {t['trace_type']} | "
            f"{t['part_of_fault_mechanism']} | {inferred.get('fault_type')}@"
            f"{inferred.get('device')} | {t['citation']} | {t['baseline']} | {t['verdict']} |"
        )
    rechecks = {
        p.stem: _read_json(p)
        for p in sorted((run_dir(name) / "recheck").glob("*.json"))
    }
    data["recheck"] = {
        k: {key: recheck_status(v, key) for key in v.get("traces") or {}}
        for k, v in rechecks.items()
    }
    _write_json(run_dir(name) / "summary.json", data)
    if rechecks:
        lines += ["", "## Recheck after fix", "", "| Trace | Status |", "|---|---|"]
        for statuses in data["recheck"].values():
            for key, status in statuses.items():
                lines.append(f"| {key} | {status} |")
    for phase, result in oracle_rows.items():
        if result:
            lines += [
                "",
                f"## Oracle ({phase}): {result['passed']}/{result['cases']} passed",
                "",
            ]
            lines += [
                "| Trial | Outcome | RCA | Failure effect |",
                "|---|---|---:|---|",
            ]
            for r in result["rows"]:
                lines.append(
                    f"| {r['trial']} | {r['outcome']} | {r['rca_accuracy']} | {r['failure_effect']} |"
                )
    (run_dir(name) / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("sample")
    sp.add_argument("name")
    sp.add_argument("--types", nargs="*")
    sp.add_argument("--max-class", default="light", choices=list(_CLASS_RANK))
    sp.add_argument("--with-healthy", action="store_true")
    for cmd in ("run", "oracle", "baseline"):
        p = sub.add_parser(cmd)
        p.add_argument("name")
        p.add_argument("--phase", default="before", choices=PHASES)
        if cmd != "baseline":
            p.add_argument("--task-id", action="extend", nargs="+", default=[])
            p.add_argument(
                "--batch-size",
                type=int,
                help="concurrent trials (default: benchmark.batch_size in config.yaml)",
            )
        if cmd == "oracle":
            p.add_argument("--batfish", action="store_true")
    for cmd in ("judge", "summary", "recheck"):
        sub.add_parser(cmd).add_argument("name")
    args = parser.parse_args()
    if args.cmd == "sample":
        sample(args.name, args.types, args.max_class, args.with_healthy)
    elif args.cmd == "run":
        run(args.name, args.phase, args.task_id, args.batch_size)
    elif args.cmd == "oracle":
        oracle(args.name, args.phase, args.task_id, args.batfish, args.batch_size)
    elif args.cmd == "baseline":
        baseline(args.name, args.phase)
    elif args.cmd == "judge":
        judge(args.name)
    elif args.cmd == "recheck":
        recheck(args.name)
    else:
        summary(args.name)


if __name__ == "__main__":
    main()
