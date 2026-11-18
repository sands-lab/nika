"""The engine grades a trial and returns ``Scores``; the frontend writes the file (#292).

``tests/fixtures/benchmark/score_cases.json`` was captured once from the code
that scored inside the engine and wrote ``eval_metrics.json`` there. Each case
records the trial's files, the frontend's infrastructure flag, and the
resulting ``eval_metrics.json``, ``score_status``, and ``run.json``
``eval_metrics``. The frontend must keep writing exactly those results.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from nika.benchmark_engine.engine import BenchmarkEngine
from nika.benchmark_engine.protocol import TrialHandle
from nika.evaluator.score_status import SCORE_KEYS
from nika.utils.session_store import SessionStore
from nika.workflows.eval.session import (
    frontend_infra_evidence,
    run_eval_metrics,
    score_trial,
)

pytestmark = pytest.mark.contract

FIXTURE = Path(__file__).parents[1] / "fixtures/benchmark/score_cases.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
# The engine's own trial files; every other trial file is the frontend's.
ENGINE_FILES = {"submission.json", "ground_truth.json"}

_opened: list[str] | None = None


def _record_reads(event: str, args: tuple) -> None:
    if _opened is None or event != "open":
        return
    path, mode, flags = args
    if isinstance(mode, str):
        reads = "r" in mode or "+" in mode
    else:
        reads = (flags or 0) & os.O_ACCMODE != os.O_WRONLY
    if reads and isinstance(path, (str, bytes, os.PathLike)):
        _opened.append(os.fsdecode(path))


sys.addaudithook(_record_reads)


@pytest.fixture(autouse=True)
def _session_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the frontend's session-index updates out of the shared runtime."""
    monkeypatch.setattr(
        SessionStore.__init__,
        "__defaults__",
        (tmp_path / "sessions", tmp_path / "sessions.db"),
    )


def _trial(root: Path, name: str) -> tuple[TrialHandle, dict]:
    case = CASES[name]
    trial = root / "trials" / name
    trial.mkdir(parents=True)
    files = {"run.json": case["run"], **case["files"]}
    for filename, content in files.items():
        if isinstance(content, str):
            text = content
        elif filename.endswith(".jsonl"):
            text = "".join(json.dumps(row) + "\n" for row in content)
        else:
            text = json.dumps(content)
        (trial / filename).write_text(text, encoding="utf-8")
    handle = TrialHandle(session_id=case["run"]["session_id"], trial_dir=str(trial))
    return handle, case


@pytest.mark.parametrize(
    "name",
    ["scored", "no_submission", "unreadable_ground_truth", "infra_evidence"],
)
def test_score_returns_the_release_metrics_and_status(
    tmp_path: Path, name: str
) -> None:
    handle, case = _trial(tmp_path, name)
    expected = case["expected"]

    scores = BenchmarkEngine().score(handle, infra_evidence=case["infra_evidence"])

    assert scores.status == expected["score_status"]
    assert scores.scoring_id == "rule-based"
    assert scores.metrics == {key: expected["eval_metrics"][key] for key in SCORE_KEYS}
    assert (scores.error is None) == (scores.status in ("scored", "no_submission"))


@pytest.mark.parametrize("path", ["engine", "offline"])
@pytest.mark.parametrize("name", sorted(CASES))
def test_frontend_writes_the_same_eval_metrics(
    tmp_path: Path, name: str, path: str
) -> None:
    handle, case = _trial(tmp_path, name)
    if path == "engine":
        score_trial(BenchmarkEngine(), handle, infra_evidence=case["infra_evidence"])
    else:
        # ``nika eval metrics`` regrades a closed trial with this NIKA's code.
        run_eval_metrics(
            session_id=handle.session_id,
            session_dir=handle.trial_dir,
            infra_evidence=case["infra_evidence"],
        )

    expected = case["expected"]
    metrics = json.loads((handle.trial_path / "eval_metrics.json").read_text())
    run_meta = json.loads((handle.trial_path / "run.json").read_text())
    assert metrics == expected["eval_metrics"]
    assert list(metrics) == list(expected["eval_metrics"])
    assert run_meta["score_status"] == expected["score_status"]
    assert run_meta["eval_metrics"] == expected["run_eval_metrics"]


@pytest.mark.parametrize("name", sorted(CASES))
def test_engine_reads_no_frontend_file(tmp_path: Path, name: str) -> None:
    global _opened
    handle, case = _trial(tmp_path, name)
    before = {p.name: p.read_bytes() for p in handle.trial_path.iterdir()}
    infra = frontend_infra_evidence(handle.trial_path, case["infra_evidence"])

    _opened = []
    try:
        BenchmarkEngine().score(handle, infra_evidence=infra)
        opened = list(_opened)
    finally:
        _opened = None

    trial = handle.trial_path.resolve()
    read = {Path(p).name for p in opened if Path(p).resolve().parent == trial}
    assert ENGINE_FILES & before.keys() <= read <= ENGINE_FILES
    # Scoring writes nothing into the trial directory.
    assert {p.name: p.read_bytes() for p in handle.trial_path.iterdir()} == before
