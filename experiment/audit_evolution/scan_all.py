"""Phase A: scan every trial and judge every group once, filling the shared caches.

Scans and group verdicts do not depend on the knowledge base, so every arm, order,
and step later reuses them. Each run goes to the alias with the most ready
replicas per run in flight; a run that ends with errors is retried later, keeping
what the caches already hold.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common as C  # noqa: E402

WORKER = """
import json, sys
from pathlib import Path
sys.path.insert(0, {here!r})
import stage_review as S
out = S.prejudge(Path({pkg!r}), {samples}, {scan_workers}, {judge_workers})
print(json.dumps(out), flush=True)
# A few groups whose judgement keeps failing are judged again, or left unclear, in phase B.
sys.exit(1 if out["scan_errors"] or out["judge_errors"] > max(1, out["groups"] // 50) else 0)
"""


def work(run: str, a, alias: str) -> int:
    log = C.ROOT / "logs" / C.MODEL_TAG / "phase_a" / a.bench / f"{run}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ | C.llm_env(alias)
    code = WORKER.format(
        here=str(HERE),
        pkg=str(C.benchmark_dir(a.bench) / run),
        samples=a.samples,
        scan_workers=a.scan_workers,
        judge_workers=a.judge_workers,
    )
    with open(log, "a") as out:
        out.write(f"--- {time.strftime('%H:%M:%S')} alias {alias}\n")
        out.flush()
        return subprocess.run(
            [str(C.PYTHON), "-c", code], env=env, stdout=out, stderr=subprocess.STDOUT
        ).returncode


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", default="nika-0.2.0-planted-t01")
    ap.add_argument("--runs-per-replica", type=int, default=1)
    ap.add_argument("--scan-workers", type=int, default=16)
    ap.add_argument("--judge-workers", type=int, default=32)
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--max-attempts", type=int, default=4)
    ap.add_argument("--runs", nargs="*")
    ap.add_argument(
        "--alias",
        help="Fixed alias (e.g. openai/gpt-6-luna) instead of gateway replicas.",
    )
    ap.add_argument(
        "--slots", type=int, default=6, help="Concurrent runs with --alias."
    )
    a = ap.parse_args()
    pending = list(a.runs or C.runs(a.bench))
    attempts = dict.fromkeys(pending, 0)
    busy: dict[str, int] = {}
    done, failed = [], []
    lock = threading.Lock()

    def finish(run, alias, rc):
        with lock:
            busy[alias] -= 1
            if rc == 0:
                done.append(run)
            elif attempts[run] < a.max_attempts:
                pending.append(run)
            else:
                failed.append(run)
            print(
                f"{time.strftime('%H:%M:%S')} {run} on {alias}: exit {rc} "
                f"({len(done)} done, {len(pending)} pending, {len(failed)} failed)",
                flush=True,
            )

    while True:
        with lock:
            if not pending and not any(busy.values()):
                break
        ready = {a.alias: a.slots} if a.alias else C.ready_replicas()
        with lock:
            for alias, n in sorted(
                ready.items(),
                key=lambda x: busy.get(x[0], 0) - x[1] * a.runs_per_replica,
            ):
                while pending and n and busy.get(alias, 0) < n * a.runs_per_replica:
                    run = pending.pop(0)
                    attempts[run] += 1
                    busy[alias] = busy.get(alias, 0) + 1
                    threading.Thread(
                        target=lambda r=run, al=alias: finish(r, al, work(r, a, al)),
                        daemon=True,
                    ).start()
        time.sleep(30)
    print(f"phase A done: {len(done)} ok, failed: {failed}")


if __name__ == "__main__":
    main()
