"""Compare cold and snapshot benchmark trials from their standard NIKA artifacts.

  uv run python experiment/vm_snapshot/compare.py <result_dir> [<result_dir> ...]

Each result dir is a `nika benchmark run --result_dir` output. Trials with a
``snapshot`` entry in run.json ran with ``nika.snapshot``. Phases come from the
lifecycle events in nika.jsonl, split by ``phase_windows`` from
experiment/profile/profile_resources.py; lab-host rows (``"origin": "remote"``)
are shifted by ``remote_clock_offset_sec`` onto the agent host's clock first.
"""

from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "profile"))
from profile_resources import PHASES, phase_windows  # noqa: E402

COLUMNS = (
    "golden",
    "restore",
    "attach",
    "deploy",
    "convergence",
    "inject",
    "agent",
    "cleanup",
    "env",
)


def _events(trial: Path, offset: float) -> list[dict]:
    rows = []
    for line in (trial / "nika.jsonl").read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("origin") == "remote" and offset:
            stamp = datetime.fromisoformat(row["timestamp"]) + timedelta(seconds=offset)
            row = {**row, "timestamp": stamp.isoformat()}
        rows.append(row)
    return rows


def _duration(events: list[dict], name: str) -> float | None:
    for row in events:
        if row.get("event") == name and row.get("duration_ms") is not None:
            return row["duration_ms"] / 1000
    return None


def trial_phases(trial: Path) -> dict | None:
    run = json.loads((trial / "run.json").read_text())
    events = _events(trial, run.get("remote_clock_offset_sec") or 0.0)
    windows = {
        name: end - start for start, end, _, name in phase_windows(events, PHASES)
    }
    snap = run.get("snapshot") or {}
    row = {
        "scenario": run.get("scenario_name"),
        "mode": "snapshot" if snap else "cold",
        "outcome": run.get("outcome"),
        "golden": snap.get("golden_sec"),
        "restore": snap.get("ready_sec"),
        "attach": snap.get("attach_sec"),
        "deploy": None if snap else _duration(events, "env_start"),
        "convergence": None if snap else _duration(events, "env_verify"),
        "inject": windows.get("inject"),
        "agent": _duration(events, "agent_end"),
        "cleanup": windows.get("cleanup"),
    }
    setup = (row["restore"] or 0) + (row["attach"] or 0) if snap else row["deploy"]
    row["env"] = round(sum(v or 0 for v in (setup, row["inject"], row["cleanup"])), 1)
    return row


def main(dirs: list[str]) -> None:
    rows = []
    for root in dirs:
        for trial in sorted(Path(root).glob("trials/*")):
            if (trial / "run.json").is_file() and (trial / "nika.jsonl").is_file():
                rows.append(trial_phases(trial))
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        groups.setdefault((row["scenario"], row["mode"]), []).append(row)
    print("| scenario | mode | n | " + " | ".join(COLUMNS) + " | outcomes |")
    print("|---|---|---:|" + "---:|" * len(COLUMNS) + "---|")
    for (scenario, mode), group in sorted(groups.items()):
        cells = []
        for col in COLUMNS:
            values = [r[col] for r in group if r[col] is not None]
            # Only the trial that built the golden VM waited for it; others reused it.
            pick = max if col == "golden" else statistics.median
            cells.append(f"{pick(values):.1f}" if values else "–")
        outcomes = ",".join(sorted({str(r["outcome"]) for r in group}))
        print(
            f"| {scenario} | {mode} | {len(group)} | "
            + " | ".join(cells)
            + f" | {outcomes} |"
        )
    print(
        "\nMedians in seconds (golden: the one build per variant). env = setup"
        " (deploy, or restore + attach) + inject + cleanup."
    )


if __name__ == "__main__":
    main(sys.argv[1:])
