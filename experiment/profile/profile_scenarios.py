"""Profile NIKA lab cost: uv run python experiment/profile/profile_scenarios.py.

Selects benchmark cases like ``nika benchmark run``: --config YAML/pool or
--release/--split (default: the working pool), narrowed by --task-id. Each distinct
lab variant (scenario, backend, size, topo, igp, bgp_mode, rpki, device_profile)
the cases deploy is profiled once; with --inject, every case instead, injecting
and verifying its failure.
No agent runs. Each case starts a lab, injects (with --inject), holds it, and
closes it under the shared NIKA ResourceSampler; phases (deploy, convergence,
inject, cleanup) come from the session's lifecycle events. Total includes setup, startup, hold and cleanup, but excludes
scheduler queue time. Like ``nika benchmark run``, --run-config (default
config/nika.yaml) supplies batch_size, heavy_batch_size and validation settings;
--batch-size and --heavy-batch-size override them. Containerlab, k8s/llmd/XRd and
topo_size l cases run after the light cases, up to heavy_batch_size at a time.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
from pathlib import Path
import signal
import time

import click

from profile_common import (
    flatten,
    median_summary,
    metric_columns,
    summary_columns,
    write_csv,
)

from nika.net_env.net_env_pool import (
    list_all_net_envs,
    scenario_fixed_topo_size,
    scenario_requires_topo_size,
)
from nika.run_config.loader import ENV_RUN_CONFIG, get_run_config
from nika.workflows.benchmark.admit import (
    CLASS_LIGHT,
    pick_admissible,
    resource_class_for_row,
    tier_limits,
)


# Lab parameters of a benchmark case row that change what start_net_env deploys.
ENV_FIELDS = ("topo", "igp", "bgp_mode", "rpki", "device_profile")


def benchmark_cases(rows: list[dict], inject: bool, specs: dict) -> list[dict]:
    """Distinct lab variants, or with ``inject`` distinct cases, of benchmark rows."""
    from nika.workflows.benchmark.healthy import is_healthy_case
    from nika.workflows.benchmark.multi_fault import (
        flatten_inject_overrides,
        row_problems,
    )
    from nika.workflows.benchmark.trials import case_key_for_row

    unique: dict[tuple, dict] = {}
    for row in rows:
        name = row["scenario"]
        backend = row.get("backend") or specs[name].supported_backends[0]
        size = row.get("topo_size") if scenario_requires_topo_size(name) else None
        case = env_case(name, backend, size, {f: row.get(f) for f in ENV_FIELDS})
        if not inject:
            unique.setdefault(tuple(case[k] for k in CASE_KEYS), case)
            continue
        problems = [] if is_healthy_case(row["problem"]) else row_problems(row)
        case.update(
            case_key=case_key_for_row(row),
            problem=row["problem"],
            problems=problems,
            # Same override shape as the benchmark runner hands inject_failure.
            inject=flatten_inject_overrides(row)
            if len(problems) > 1
            else dict(row.get("inject") or {}),
        )
        unique.setdefault(case["case_key"], case)
    return list(unique.values())


def env_case(
    name: str, backend: str, size: str | None, params: dict | None = None
) -> dict:
    return dict(
        scenario=name,
        backend=backend,
        size_argument=size,
        topo_size=size or scenario_fixed_topo_size(name),
        **{field: (params or {}).get(field) for field in ENV_FIELDS},
    )


CASE_KEYS = ("scenario", "backend", "topo_size", *ENV_FIELDS)


def profile_case(
    case: dict, output: str, interval: float, hold: float, sid: str
) -> dict:
    from profile_resources import ResourceSampler, summarize
    from nika.remote.config import is_remote_enabled
    from nika.workflows.benchmark._trial_worker import _raise_on_sigterm
    from nika.workflows.env.start import start_net_env
    from nika.workflows.failure.inject import inject_failure
    from nika.workflows.session.close import close_session

    # The parent forwards interrupts via the benchmark worker termination path.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, _raise_on_sigterm)
    if is_remote_enabled():
        raise ValueError("Profiling requires local Docker; disable remote mode.")
    directory = Path(output) / sid
    directory.mkdir(parents=True)
    result = dict(case, session_id=sid, status="ok")
    started = time.perf_counter()
    sampler = ResourceSampler(sid, directory, interval=interval)
    sampler.start()
    try:
        start_net_env(
            case["scenario"],
            case["size_argument"],
            backend=case["backend"],
            **{field: case[field] for field in ENV_FIELDS},
            session_id=sid,
            session_dir=str(directory),
        )
        if case.get("problems"):
            inject_failure(
                case["problems"], session_id=sid, param_overrides=case["inject"]
            )
        time.sleep(hold)
    except (KeyboardInterrupt, SystemExit):
        result.update(status="aborted")
    except Exception as exc:
        result.update(status="error", error=f"{type(exc).__name__}: {exc}")
    finally:
        try:
            close_session(
                session_id=sid,
                session_dir=directory,
                status="aborted"
                if result["status"] == "aborted"
                else ("error" if result["status"] == "error" else "finished"),
            )
            result["cleanup_ok"] = True
        except Exception as exc:
            result.update(status="error", cleanup_ok=False, cleanup_error=str(exc))
        sampler.stop()
        result["total_seconds"] = time.perf_counter() - started
    result.update(flatten(summarize(directory)))
    if not result["total_peak_containers"] and result["status"] != "aborted":
        result.update(
            status="error", sampling_error="No container resource samples collected"
        )
    (directory / "profile.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", type=Path, help="Benchmark YAML or pool")
    parser.add_argument("--release", help="Frozen release version or ref")
    parser.add_argument("--split", help="Release split: dev or test")
    parser.add_argument(
        "--task-id",
        dest="task_ids",
        action="append",
        help="Profile only this task id. Repeatable",
    )
    parser.add_argument(
        "--inject",
        action="store_true",
        help="Profile every case, injecting its failure",
    )
    parser.add_argument(
        "--run-config",
        default=os.environ.get(ENV_RUN_CONFIG),
        help="Path to config/nika.yaml (default: config/nika.yaml)",
    )
    parser.add_argument(
        "--batch-size", type=int, help="Default: benchmark.batch_size in run config"
    )
    parser.add_argument(
        "--heavy-batch-size",
        type=int,
        help="Max concurrent Containerlab, k8s/llmd/XRd and size l cases "
        "(default: benchmark.heavy_batch_size in run config)",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="Profile every case N times, queued in rounds; summary.csv keeps medians",
    )
    parser.add_argument("--sample-interval", type=float, default=1)
    parser.add_argument("--hold-seconds", type=float, default=2)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("results") / f"scenario-profile-{time.time_ns()}",
    )
    parser.add_argument(
        "--list", action="store_true", help="Print case plan without deploying"
    )
    args = parser.parse_args()
    if args.repeats < 1 or args.sample_interval <= 0 or args.hold_seconds < 0:
        parser.error("repeats >= 1, sample-interval > 0, hold-seconds >= 0 required")
    from nika.cli.commands.benchmark import load_catalog_rows
    from nika.workflows.benchmark.trials import resolve_catalog_row

    try:
        rows = load_catalog_rows(
            config=args.config,
            release=args.release,
            split=args.split,
            run_config=args.run_config,
        )
        rows = [resolve_catalog_row(rows, t) for t in args.task_ids or ()] or rows
    except (ValueError, click.ClickException) as exc:
        parser.error(str(exc))
    except click.exceptions.Exit as exc:  # release errors are already printed
        return exc.exit_code
    # load_catalog_rows exported the run config path, so workers load it too.
    config = get_run_config()
    batch_size = args.batch_size or config.benchmark.batch_size
    heavy_batch_size = args.heavy_batch_size or config.benchmark.heavy_batch_size
    if batch_size < 1 or heavy_batch_size < 1:
        parser.error("batch size and heavy batch size >= 1 required")
    cases = benchmark_cases(rows, args.inject, list_all_net_envs())
    # Admission stops at a waiting heavy case, so queue light cases first.
    cases.sort(key=lambda case: resource_class_for_row(case) != CLASS_LIGHT)
    # Rounds keep repeats of one case apart instead of running them side by side.
    cases = [
        dict(case, repeat=repeat)
        for repeat in range(1, args.repeats + 1)
        for case in cases
    ]
    if args.list:
        print(json.dumps(cases, indent=2))
        return 0
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "plan.json").write_text(
        json.dumps(
            dict(
                source=dict(
                    config=args.config and str(args.config),
                    release=args.release,
                    split=args.split,
                    task_ids=args.task_ids,
                ),
                cases=cases,
                run_config=os.environ[ENV_RUN_CONFIG],
                batch_size=batch_size,
                heavy_batch_size=heavy_batch_size,
                repeats=args.repeats,
                sample_interval=args.sample_interval,
                hold_seconds=args.hold_seconds,
                resource_scope="lab container cgroups, NIKA worker process tree",
                validation_depth=config.nika.runtime_validation.depth,
                static_validation=config.nika.static_validation.enabled,
            ),
            indent=2,
        )
        + "\n"
    )
    results = []
    pending = list(cases)
    running = {}
    interrupted = False
    from nika.utils.session_id import make_session_id
    from nika.workflows.benchmark.run import (
        _register_trial_proc,
        _unregister_trial_proc,
        cleanup_benchmark_interrupt,
    )
    from nika.workflows.session.close import close_session

    limits = tier_limits(batch_size=batch_size, heavy_batch_size=heavy_batch_size)
    in_flight: dict[str, int] = {}
    context = multiprocessing.get_context("spawn")
    try:
        while pending or running:
            while pending and len(running) < max(limits.values()):
                index = pick_admissible(
                    pending,
                    in_flight=in_flight,
                    limits=limits,
                    classify=resource_class_for_row,
                )
                if index is None:
                    break
                case = pending.pop(index)
                cls = resource_class_for_row(case)
                in_flight[cls] = in_flight.get(cls, 0) + 1
                sid = make_session_id(session_tag="test")
                proc = context.Process(
                    target=profile_case,
                    args=(
                        case,
                        str(args.output),
                        args.sample_interval,
                        args.hold_seconds,
                        sid,
                    ),
                )
                # Register before starting so an interrupt cannot lose this worker.
                _register_trial_proc(proc)
                running[proc] = (case, sid)
                proc.start()
            for proc in list(running):
                if proc.is_alive():
                    continue
                proc.join()
                _unregister_trial_proc(proc)
                case, sid = running.pop(proc)
                cls = resource_class_for_row(case)
                in_flight[cls] -= 1
                path = args.output / sid / "profile.json"
                result = (
                    json.loads(path.read_text())
                    if path.exists()
                    else dict(
                        case,
                        session_id=sid,
                        status="error",
                        error=f"Worker exited with code {proc.exitcode} without a profile",
                    )
                )
                if not path.exists():
                    try:
                        close_session(
                            session_id=sid, session_dir=path.parent, status="error"
                        )
                        result["cleanup_ok"] = True
                    except Exception as exc:
                        result.update(cleanup_ok=False, cleanup_error=str(exc))
                results.append(result)
                with (args.output / "profiles.jsonl").open("a") as stream:
                    stream.write(json.dumps(result) + "\n")
                print(
                    f"{case['scenario']} [{case['backend']}/{case['topo_size']}] "
                    f"#{case['repeat']}: {result['status']}",
                    flush=True,
                )
            if running:
                time.sleep(0.1)
    except KeyboardInterrupt:
        interrupted = True
        print("Interrupted: stopping workers and undeploying sessions.", flush=True)
        cleanup_benchmark_interrupt(args.output, signal_workers=True)
        for case, sid in running.values():
            path = args.output / sid / "profile.json"
            result = (
                json.loads(path.read_text())
                if path.exists()
                else dict(
                    case,
                    session_id=sid,
                    status="aborted",
                    error="Worker stopped; see session run.json for cleanup status",
                )
            )
            results.append(result)
            with (args.output / "profiles.jsonl").open("a") as stream:
                stream.write(json.dumps(result) + "\n")
    except BaseException:
        cleanup_benchmark_interrupt(args.output, signal_workers=True)
        raise
    metrics = ["total_seconds", *metric_columns()]
    keys = (*CASE_KEYS, "case_key", "problem") if args.inject else CASE_KEYS
    write_csv(
        args.output / "profiles.csv",
        results,
        [*keys, "repeat", "status", *metrics, "cleanup_ok", "session_id", "error"],
    )
    write_csv(
        args.output / "summary.csv",
        median_summary(results, keys, metrics),
        summary_columns(keys, metrics),
    )
    print(f"Results: {args.output}")
    return 130 if interrupted else int(any(r["status"] != "ok" for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
