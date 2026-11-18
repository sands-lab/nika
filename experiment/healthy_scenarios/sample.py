"""Sample administrative state at the same delays after each ready boundary."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

SCRIPT = (Path(__file__).parent.parent / "convergence/node_dump.sh").read_text()


def sample(root: Path, ready: float, export) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for delay in (0, 60):
        remaining = ready + delay - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        started = time.monotonic()
        nodes = export()
        (root / f"dump_{delay}.json").write_text(
            json.dumps(
                {
                    "delay_sec": delay,
                    "dump_started_after_ready_sec": started - ready,
                    "dump_duration_sec": time.monotonic() - started,
                    "node_dump_sha256": hashlib.sha256(SCRIPT.encode()).hexdigest(),
                    "nodes": nodes,
                }
            )
        )
