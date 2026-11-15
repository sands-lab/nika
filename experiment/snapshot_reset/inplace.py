# ruff: noqa: E402
"""NIKA in-place reset: deploy each environment variant once, then per task inject, undo, re-verify.

This mirrors the in-place recovery of NetOpsBench and NIKA's own scenario validation
(nika.workflows.validation.scenario): one long-lived lab, the fault's recover_fault()
inverse after each task, and the startup verification as the "network is healthy again"
gate. The agent is skipped; every timing excludes it.

  NIKA_RUN_CONFIG=~/reset-exp/nika/native.yaml uv run --project ~/nika python inplace.py tasks.yaml out.jsonl
"""

from __future__ import annotations

import json
import signal
import sys
import time
from itertools import groupby
from pathlib import Path

import yaml

from nika.runtime.factory import runtime_for_session
from nika.validation.presence import bound_injected_problem
import nika.workflows.validation.scenario as _scenario
from nika.workflows.validation.scenario import restore_link_routes

# restore_link_routes parses link addresses with startup_addresses, which matches only
# "ip addr add". k8s_lab's hand-written startup files use "ip address add", so accept both
# here (nothing in NIKA's own workflows passes those files to it).
_scenario.startup_addresses = lambda startup, *, include_loopback=False: __import__(
    "re"
).findall(
    r"ip addr(?:ess)? add ([0-9./]+) dev ((?:eth|br)[0-9]+"
    + (r"|lo" if include_loopback else "")
    + r")\b",
    startup,
)
from nika.net_env.verify import verify_lab_with_retry
from nika.workflows.env.start import start_net_env
from nika.workflows.failure.inject import inject_failure
from nika.workflows.session.close import (
    close_session,
    load_session_meta_for_close,
    session_net_env,
)

RESULT_DIR = str(Path.home() / "reset-exp/nika/results_inplace")


def timed(fn):
    t = time.monotonic()
    value = fn()
    return value, round(time.monotonic() - t, 2)


def env_events(session_dir: str) -> dict[str, float]:
    """duration_ms of the deploy-phase events NIKA logs, in seconds."""
    out = {}
    path = Path(session_dir) / "nika.jsonl"
    for line in path.read_text().splitlines() if path.is_file() else []:
        e = json.loads(line)
        if e.get("event") in ("env_start", "env_verify", "env_workload") and e.get(
            "duration_ms"
        ):
            out.setdefault(e["event"] + "_s", round(e["duration_ms"] / 1000, 2))
    return out


def main(tasks_path: str, out_path: str) -> None:
    rows = yaml.safe_load(open(tasks_path))["cases"]
    # One lab per environment variant: group the tasks of each variant together.
    order = {}
    for r in rows:
        order.setdefault((r["scenario"], r.get("topo_size")), len(order))
    rows.sort(key=lambda r: order[(r["scenario"], r.get("topo_size"))])
    # Close the lab (finally) when stopped with SIGTERM.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    out = open(out_path, "a")

    def emit(record):
        out.write(json.dumps(record) + "\n")
        out.flush()
        print(json.dumps(record), flush=True)

    variant = lambda r: (r["scenario"], r.get("topo_size"))  # noqa: E731
    for (scenario, size), group in groupby(rows, key=variant):
        sid, setup_s = timed(
            lambda: start_net_env(
                scenario,
                size,
                redeploy=True,
                result_dir=RESULT_DIR,
                session_tag="inplace",
            )
        )
        meta = load_session_meta_for_close(sid)
        env = session_net_env(meta)
        runtime = runtime_for_session(meta)
        emit(
            {
                "kind": "setup",
                "scenario": scenario,
                "size": size,
                "setup_s": setup_s,
                **env_events(meta["session_dir"]),
            }
        )
        try:
            for row in group:
                rec = {
                    "kind": "task",
                    "scenario": scenario,
                    "size": size,
                    "problem": row["problem"],
                }
                try:
                    if row["problem"] != "healthy":
                        _, rec["inject_s"] = timed(
                            lambda: inject_failure(
                                [row["problem"]],
                                session_id=sid,
                                param_overrides=row.get("inject") or {},
                                expected_root_causes=row.get("root_causes"),
                            )
                        )
                        problem, params = bound_injected_problem(sid)
                        result, rec["recover_s"] = timed(
                            lambda: problem.recover_fault(params) or {}
                        )
                        host, intf = (
                            getattr(params, "host_name", None),
                            getattr(params, "intf_name", None),
                        )
                        if host and intf:
                            # recover_fault raises the interface again, but the kernel already dropped
                            # the routes through it. Reapply them, as NIKA's scenario validation does.
                            restored, rec["route_restore_s"] = timed(
                                lambda: restore_link_routes(env, runtime, host, intf)
                            )
                            rec["routes_restored"] = len(restored)
                        rec["recover_verified"] = result.get("verified")
                    _, rec["reverify_s"] = timed(lambda: verify_lab_with_retry(env))
                    # The full lab verification (runtime_validation.depth: full), as a stricter gate.
                    full, rec["reverify_full_s"] = timed(lambda: env.verify_lab())
                    rec["full_verified"] = bool((full or {}).get("verified", True))
                    rec["ok"] = rec.get("recover_verified") is not False
                except Exception as exc:  # noqa: BLE001 - recorded per task
                    rec.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:400])
                emit(rec)
        finally:
            _, teardown_s = timed(lambda: close_session(session_id=sid))
            emit(
                {
                    "kind": "teardown",
                    "scenario": scenario,
                    "size": size,
                    "teardown_s": teardown_s,
                }
            )


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
