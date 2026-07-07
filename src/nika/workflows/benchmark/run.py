"""Batch or single-case benchmark runs (env → inject → agent → eval)."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from pydantic import ValidationError

from nika.config import BENCHMARK_DIR
from nika.utils.session import Session
from nika.utils.session_artifacts import RUN_FILENAME
from nika.utils.session_store import SessionStore
from nika.net_env.net_env_pool import scenario_requires_topo_size
from nika.problems.prob_pool import get_problem_instance
from nika.workflows.agent.run import start_agent
from nika.workflows.benchmark.load_config import load_benchmark_yaml
from nika.workflows.benchmark.resume import (
    benchmark_row_fingerprint,
    benchmark_row_from_case,
    scan_benchmark_cases,
)
from nika.workflows.env.start import start_net_env
from nika.workflows.eval.session import eval_results
from nika.workflows.failure.inject import inject_failure
from nika.workflows.session.close import close_session

_BENCHMARK_DONE_PREFIX = "benchmark_done "
_BENCHMARK_DONE_RE = re.compile(
    r"benchmark_done session_id=(\S+) scenario=(\S+) problem=(\S+) session_dir=(\S+)"
)


def default_benchmark_yaml_path() -> str:
    return str(BENCHMARK_DIR / "benchmark_selected.yaml")


def validate_inject_params(
    problem: str,
    scenario: str,
    topo_size: str,
    params: dict[str, str],
) -> None:
    """Raise ValueError if inject params do not satisfy the problem schema."""
    if not params:
        raise ValueError(
            f"Missing inject parameters for {problem!r}. "
            f"Use --config with a YAML case or pass complete --set key=value flags. "
            f"Run `nika failure describe {problem}` for required fields."
        )

    kwargs: dict = {}
    if topo_size:
        kwargs["topo_size"] = topo_size
    problem_inst = get_problem_instance(
        problem_names=[problem],
        scenario_name=scenario,
        **kwargs,
    )
    params_class = getattr(type(problem_inst), "Params", None)
    if params_class is None:
        if params:
            raise ValueError(f"Problem {problem!r} does not accept inject parameters.")
        return
    try:
        params_class(**params)
    except ValidationError as exc:
        raise ValueError(
            f"Invalid or incomplete inject parameters for {problem!r}: {exc}. "
            f"Run `nika failure describe {problem}` for required fields."
        ) from exc


def _benchmark_row_cli_args(
    row: dict,
    *,
    agent_type: str,
    llm_provider: str | None,
    model: str | None,
    max_steps: int | None,
    run_judge: bool,
    judge_llm_provider: str | None,
    judge_model: str | None,
    result_dir: str | None = None,
    session_tag: str | None = None,
) -> list[str]:
    args = [
        row["scenario"],
        "--problem",
        row["problem"],
        "-a",
        agent_type,
    ]
    if llm_provider:
        args += ["-p", llm_provider]
    if model:
        args += ["-m", model]
    if max_steps is not None:
        args += ["-n", str(max_steps)]
    topo = row.get("topo_size") or ""
    if topo:
        args += ["-s", topo]
    inject = row.get("inject") or {}
    for key, value in inject.items():
        args += ["--set", f"{key}={value}"]
    if run_judge:
        args += [
            "--judge",
            "--judge-provider",
            judge_llm_provider,
            "--judge-model",
            judge_model,
        ]
    if result_dir:
        args += ["--result_dir", result_dir]
    if session_tag:
        args += ["--session-tag", session_tag]
    return args


def _run_benchmark_row_subprocess(
    row: dict,
    *,
    agent_type: str,
    llm_provider: str | None,
    model: str | None,
    max_steps: int | None,
    run_judge: bool,
    judge_llm_provider: str | None,
    judge_model: str | None,
    result_dir: str | None = None,
    session_tag: str | None = None,
    case_timeout: int = 0,
) -> None:
    """Run one YAML row via a subprocess for thread-safe parallel batch execution.

    ``case_timeout`` > 0 arms a hard per-case watchdog: the whole subprocess
    group is killed when it expires, so one hung case (frozen agent, stuck
    tool call) cannot stall the run forever.
    """
    cli_args = _benchmark_row_cli_args(
        row,
        agent_type=agent_type,
        llm_provider=llm_provider,
        model=model,
        max_steps=max_steps,
        run_judge=run_judge,
        judge_llm_provider=judge_llm_provider,
        judge_model=judge_model,
        result_dir=result_dir,
        session_tag=session_tag,
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "nika.cli.main", "benchmark", "run", *cli_args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,  # own process group: the watchdog kill reaps
    )                            # docker clients/agents too, not just the CLI
    timed_out = False
    try:
        output, _ = proc.communicate(timeout=case_timeout if case_timeout > 0 else None)
    except subprocess.TimeoutExpired:
        timed_out = True
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except ProcessLookupError:
                break
            try:
                proc.wait(timeout=15)
                break
            except subprocess.TimeoutExpired:
                continue
        output, _ = proc.communicate()

    scenario = row.get("scenario", "?")
    problem = row.get("problem", "?")
    if timed_out:
        raise RuntimeError(
            f"[{scenario}/{problem}] case exceeded --case-timeout ({case_timeout}s) "
            f"and was killed. Its lab may be leaked — check `nika session ps`. "
            f"Output tail:\n{(output or '')[-2000:]}"
        )
    if proc.returncode != 0:
        raise RuntimeError(
            f"[{scenario}/{problem}] `nika benchmark run {' '.join(cli_args)}` "
            f"exited {proc.returncode}:\n{output}"
        )
    if output:
        print(output, end="" if output.endswith("\n") else "\n")


def _run_benchmark_batch_parallel(
    indexed_rows: list[tuple[int, dict]],
    *,
    continue_on_error: bool = False,
    **shared_kwargs,
) -> list[str]:
    """Run indexed rows simultaneously (one subprocess each), then return.

    Returns the error messages of failed rows. With ``continue_on_error``
    False (historic behavior) the first failure raises; otherwise failures
    are reported and the batch keeps going.
    """
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=len(indexed_rows)) as pool:
        futures = [
            pool.submit(_run_benchmark_row_subprocess, row, **shared_kwargs)
            for _index, row in indexed_rows
        ]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:  # noqa: BLE001 - row errors are aggregated
                if not continue_on_error:
                    raise
                print(f"CASE FAILED (continuing): {e}")
                failures.append(str(e))
    return failures


def run_single_case(
    problem: str,
    scenario: str,
    topo_size: str,
    agent_type: str,
    llm_provider: str | None,
    model: str | None,
    max_steps: int | None,
    *,
    inject_params: dict[str, str],
    run_judge: bool = False,
    judge_llm_provider: str | None = None,
    judge_model: str | None = None,
    result_dir: str | None = None,
    session_tag: str | None = None,
) -> tuple[str, Path]:
    """Run one benchmark case (env → inject → agent → eval).

    Returns:
        The session id and session directory for the completed run.
    """
    print(
        f"Running benchmark for Problem: {problem}, Scenario: {scenario}, Topo Size: {topo_size}"
    )

    size = topo_size if topo_size else None
    if scenario_requires_topo_size(scenario) and not size:
        raise ValueError(
            f"Scenario '{scenario}' requires a non-empty topology size (-s s|m|l)."
        )
    if not scenario_requires_topo_size(scenario):
        size = None

    validate_inject_params(problem, scenario, topo_size or "", inject_params)
    params = dict(inject_params)

    session_id = start_net_env(
        scenario, size, redeploy=True, result_dir=result_dir, session_tag=session_tag
    )
    session_dir = Path(SessionStore().get_session(session_id)["session_dir"])

    try:
        inject_failure(
            problem_names=[problem], session_id=session_id, param_overrides=params
        )

        row = benchmark_row_from_case(
            scenario=scenario,
            problem=problem,
            topo_size=topo_size,
            inject_params=params,
        )
        Session().load_running_session(session_id=session_id).update_session(
            "benchmark_fingerprint",
            benchmark_row_fingerprint(row),
        )

        start_agent(
            agent_type=agent_type,
            llm_provider=llm_provider,
            model=model,
            max_steps=max_steps,
            session_id=session_id,
            stream_output=False,
        )

        eval_results(
            session_id=session_id,
            run_judge=run_judge,
            judge_llm_provider=judge_llm_provider,
            judge_model=judge_model,
        )
    except BaseException:
        # A failed case must not strand a "running" session with its Kathara
        # lab still deployed (leaked labs burn CPU and skew later cases).
        # eval_results closes the session on the success path.
        try:
            close_session(session_id=session_id, undeploy=True)
            print(f"cleaned up failed session {session_id} (lab undeployed)")
        except Exception as cleanup_error:  # noqa: BLE001 - best effort
            print(f"WARNING: could not clean up session {session_id}: {cleanup_error}")
        # If the failure happened after close_session already marked the run
        # "finished" (e.g. during evaluation), resume would skip this case
        # forever even though its artifacts are incomplete.
        try:
            run_path = session_dir / RUN_FILENAME
            run_meta = json.loads(run_path.read_text(encoding="utf-8"))
            if run_meta.get("status") == "finished":
                run_meta["status"] = "error"
                run_path.write_text(json.dumps(run_meta, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001 - best effort
            pass
        raise

    print(
        f"{_BENCHMARK_DONE_PREFIX}session_id={session_id} scenario={scenario} "
        f"problem={problem} session_dir={session_dir}"
    )
    return session_id, session_dir


def run_benchmark_from_yaml(
    benchmark_file: str,
    agent_type: str,
    llm_provider: str | None,
    model: str | None,
    max_steps: int | None,
    *,
    batch_size: int = 1,
    run_judge: bool = False,
    judge_llm_provider: str | None = None,
    judge_model: str | None = None,
    result_dir: str | None = None,
    resume: bool = True,
    session_tag: str | None = None,
    case_timeout: int = 0,
    continue_on_error: bool = False,
    retry_passes: int = 0,
) -> None:
    """
    Run benchmark cases defined in a YAML file.

    Each case must include scenario, problem, optional topo_size, and inject params.

    All rows are scanned first against existing session dirs under ``--result_dir``:
    completed cases are skipped and incomplete ones are cleaned. Remaining cases run
    sequentially when ``batch_size == 1`` (default), or in parallel chunks when
    ``batch_size > 1``. Re-run the same command to resume after an interruption.

    ``case_timeout`` > 0 arms a hard per-case watchdog (each row then runs in
    its own subprocess even with ``batch_size == 1``). ``continue_on_error``
    keeps the run going past failed rows and summarizes them at the end
    instead of aborting on the first failure. ``retry_passes`` > 0 then
    automatically re-scans and retries the failed cases up to that many extra
    passes (implies ``continue_on_error``); retries stop early when a full
    pass completes no new case, so a deterministic bug cannot burn passes.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    if retry_passes < 0:
        raise ValueError("retry_passes must be >= 0")
    if retry_passes and not continue_on_error:
        # A retry loop is pointless if the first failure aborts the run.
        continue_on_error = True

    rows = load_benchmark_yaml(benchmark_file)

    if not rows:
        print(f"No benchmark rows found in {benchmark_file}")
        return

    _shared_kwargs = dict(
        agent_type=agent_type,
        llm_provider=llm_provider,
        model=model,
        max_steps=max_steps,
        run_judge=run_judge,
        judge_llm_provider=judge_llm_provider,
        judge_model=judge_model,
        result_dir=result_dir,
        session_tag=session_tag,
    )

    def _run_pending(pending: list[int]) -> list[str]:
        failures: list[str] = []
        # The watchdog needs a killable process per case, so a timeout forces
        # the subprocess path even for sequential runs.
        if batch_size == 1 and case_timeout <= 0:
            for index in pending:
                row = rows[index]
                label = f"[{index + 1}/{len(rows)}] {row['scenario']}/{row['problem']}"
                print(f"{label} running")
                try:
                    run_single_case(
                        problem=row["problem"],
                        scenario=row["scenario"],
                        topo_size=row.get("topo_size") or "",
                        inject_params=row["inject"],
                        **_shared_kwargs,
                    )
                except Exception as e:  # noqa: BLE001 - row errors are aggregated
                    if not continue_on_error:
                        raise
                    print(
                        f"CASE FAILED (continuing): [{row['scenario']}/{row['problem']}] {e}"
                    )
                    failures.append(f"[{row['scenario']}/{row['problem']}] {e}")
        else:
            for chunk_start in range(0, len(pending), batch_size):
                chunk_indices = pending[chunk_start : chunk_start + batch_size]
                indexed_rows = [(index, rows[index]) for index in chunk_indices]
                first = chunk_indices[0] + 1
                last = chunk_indices[-1] + 1
                print(
                    f"[batch {chunk_start // batch_size + 1}] running {len(chunk_indices)} session(s) in parallel "
                    f"(rows {first}–{last} of {len(rows)})"
                )
                failures += _run_benchmark_batch_parallel(
                    indexed_rows,
                    continue_on_error=continue_on_error,
                    case_timeout=case_timeout,
                    **_shared_kwargs,
                )
        return failures

    failures: list[str] = []
    previous_pending: int | None = None
    for attempt in range(retry_passes + 1):
        _results_root, pending = scan_benchmark_cases(
            rows=rows,
            result_dir=result_dir,
            # Retry passes must skip completed cases regardless of --no-resume.
            resume=resume or attempt > 0,
        )
        if not pending:
            if attempt > 0:
                print("\nAll benchmark cases completed after retries.")
            return
        if attempt > 0:
            if previous_pending is not None and len(pending) >= previous_pending:
                print(
                    f"\nRetry made no progress ({len(pending)} case(s) still "
                    "failing deterministically); stopping retries."
                )
                break
            print(
                f"\n[retry {attempt}/{retry_passes}] retrying {len(pending)} failed case(s)"
            )
        previous_pending = len(pending)
        failures = _run_pending(pending)
        if not failures:
            if attempt > 0:
                print("\nAll benchmark cases completed after retries.")
            return

    if failures:
        print(
            f"\n{len(failures)} case(s) still FAILED "
            "(re-run the same command with --resume to retry only these):"
        )
        for message in failures:
            print(f"  - {message.splitlines()[0]}")
