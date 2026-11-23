"""Collect recorded tasks from NIKA run folders into one dataset folder.

    uv run python scripts/offline_recording/export_dataset.py \
        results/offline-run-1 results/offline-run-2 -o dataset/

Layout written:

    dataset/
      tasks.csv                  one row per task (no labels)
      labels.csv                 one row per task: the answer
      tasks/<task_id>/           model input
      labels/<task_id>.json      answer and injection timing
      before_fault/<task_id>/    device state 30 s before the fault (optional input)

Task IDs are ``<network>-<8 hex>``; they do not reveal the fault. When the same
task was recorded in several run folders, the last folder given wins.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import posixpath
import re
import shutil
import subprocess
from pathlib import Path

# Older recordings used other folder names.
_LEGACY = {"offline_bundle": "recording"}


def _recordings(run_dirs: list[Path]) -> dict[str, tuple[Path, Path]]:
    found: dict[str, tuple[Path, Path]] = {}
    for run in run_dirs:
        for trial in sorted((run / "trials").glob("*")):
            for name in ("recording", *_LEGACY):
                rec = trial / name
                label = rec / ("_label" if (rec / "_label").exists() else "_answer")
                if (label / "recording.json").exists():
                    found[trial.name] = (trial, rec)
                    break
    return found


HERE = Path(__file__).resolve().parent
REPO_URL = "https://github.com/sands-lab/nika"
CARD_HEADER = """---
license: mit
pretty_name: NIKA Incident Recordings
tags:
- network
- troubleshooting
- incident
- telemetry
- nika
---

"""
_LINK = re.compile(r"(!?)\[([^\]]*)\]\(([^)\s]+)\)")


def write_card(out: Path) -> None:
    """Write the dataset card: this folder's README with figures copied in.

    Other relative links point to GitHub at the commit that exported the
    dataset, so they keep matching the data.
    """
    ref = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=HERE,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    repo_root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=HERE,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    base = HERE.relative_to(repo_root).as_posix()
    (out / "figures").mkdir(exist_ok=True)
    used: dict[str, str] = {}

    def link(match: re.Match) -> str:
        bang, text, target = match.groups()
        if target.startswith(("http://", "https://", "#", "mailto:")):
            return match.group(0)
        if bang:
            # Same-named images from different folders must not overwrite each other.
            name = used.setdefault(target, Path(target).name)
            if list(used.values()).count(name) > 1:
                name = used[target] = target.replace("/", "-")
            shutil.copy2(HERE / target, out / "figures" / name)
            return f"![{text}](figures/{name})"
        path = posixpath.normpath(posixpath.join(base, target))
        return f"[{text}]({REPO_URL}/blob/{ref}/{path})"

    body = _LINK.sub(link, (HERE / "README.md").read_text())
    (out / "README.md").write_text(CARD_HEADER + body)


def _segments(description: str) -> list[list[dict[str, str]]]:
    from nika.agents.recorder.agent import _topology_segments

    return _topology_segments(description)


def _copy_model_input(rec: Path, dest: Path) -> None:
    for item in rec.iterdir():
        if item.name.startswith("_"):
            continue
        target = dest / ("devices" if item.name == "nodes" else item.name)
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)


def export(run_dirs: list[Path], out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    (out / "tasks").mkdir(parents=True)
    (out / "labels").mkdir()
    task_rows, label_rows = [], []
    for trial_name, (trial, rec) in sorted(_recordings(run_dirs).items()):
        run = json.loads((trial / "run.json").read_text())
        network = run["scenario_name"]
        task_id = f"{network}-{hashlib.sha1(trial_name.encode()).hexdigest()[:8]}"
        dest = out / "tasks" / task_id
        dest.mkdir()
        _copy_model_input(rec, dest)

        task = json.loads((dest / "task.json").read_text())
        if "nodes" in task:
            task["devices"] = task.pop("nodes")
        task.pop("links", None)
        task.setdefault("segments", _segments(task.get("description", "")))
        task = {"task_id": task_id, "network": network, **task}
        task["topology_size"] = run.get("scenario_topo_size")
        (dest / "task.json").write_text(json.dumps(task, indent=1) + "\n")

        label_dir = rec / ("_label" if (rec / "_label").exists() else "_answer")
        gt = json.loads((label_dir / "ground_truth.json").read_text())
        rec_meta = json.loads((label_dir / "recording.json").read_text())
        label = {
            "task_id": task_id,
            "is_anomaly": gt.get("is_anomaly", False),
            "fault_category": gt.get("failure_domain") or None,
            "root_causes": [
                {"fault_type": c["fault_type"], "component": c["resource_id"]}
                for c in gt.get("root_causes", [])
            ],
            "fault_injected_at": rec_meta.get("injection_finished"),
        }
        (out / "labels" / f"{task_id}.json").write_text(
            json.dumps(label, indent=1) + "\n"
        )

        before = rec / (
            "_before_fault"
            if (rec / "_before_fault").exists()
            else "_baseline_snapshot"
        )
        if before.exists():
            src = before / ("devices" if (before / "devices").exists() else "nodes")
            shutil.copytree(src, out / "before_fault" / task_id / "devices")

        timeline = json.loads((dest / "timeline.json").read_text())
        timeline["note"] = timeline.get("note", "").replace("nodes/", "devices/")
        (dest / "timeline.json").write_text(json.dumps(timeline, indent=2) + "\n")
        task_rows.append(
            {
                "task_id": task_id,
                "network": network,
                "topology_size": task["topology_size"] or "",
                "devices": len(task["devices"]),
                "segments": len(task["segments"]),
                "telemetry": "+".join(task.get("telemetry") or []),
                "recording_start": timeline["recording_start"],
                "alert_time": timeline["alert_time"],
                "recording_end": timeline["recording_end"],
            }
        )
        label_rows.append(
            {
                "task_id": task_id,
                "is_anomaly": label["is_anomaly"],
                "fault_category": label["fault_category"] or "",
                "fault_type": ";".join(c["fault_type"] for c in label["root_causes"]),
                "faulty_component": ";".join(
                    c["component"] for c in label["root_causes"]
                ),
            }
        )
    for name, rows in (("tasks.csv", task_rows), ("labels.csv", label_rows)):
        if not rows:
            continue
        with (out / name).open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    write_card(out)
    print(f"wrote {len(task_rows)} tasks to {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="+", type=Path, help="NIKA result folders")
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()
    export(args.runs, args.output)


if __name__ == "__main__":
    main()
