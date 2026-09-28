"""Unit tests for benchmark run console helpers (Issue #53)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from nika.workflows.benchmark.display import (
    BenchmarkProgress,
    RunPlan,
    confirm_run,
    format_run_plan,
    quiet_third_party_logging,
    read_trial_metrics,
)
from nika.workflows.benchmark.trials import expand_trials, scan_trials, trial_dir
from tests.benchmark.trial_helpers import write_valid_trial


def test_format_run_plan_lists_pending_and_skips() -> None:
    text = format_run_plan(
        RunPlan(
            total_trials=5,
            pending_count=2,
            skipped_count=3,
            agent_type="mock",
            model="mock-v1",
            result_dir="/tmp/results",
            pending_labels=[
                "simple_bgp/link_down s src=r1 dst=r2 t01",
                "dc_clos/link_flap m t01",
            ],
            skipped_labels=[
                "dc_clos/link_down m t01",
                "dc_clos/link_down m t02",
                "dc_clos/mtu_mismatch m t01",
            ],
            header="Release demo",
            batch_size=2,
            case_count=2,
            n_trials=1,
        ),
        max_labels=24,
    )
    assert "Release demo" in text
    assert "2/5 run(s) remaining" in text
    assert "2 case(s) × 1 trial(s)/case" in text
    assert "3 already complete" in text
    assert "batch_size=2" in text
    assert "serialize_heavy=true" in text
    assert "Agent: mock  model=mock-v1" in text
    assert "Results: /tmp/results" in text
    assert "Done (3):" in text
    assert "  - dc_clos/link_down m t01" in text
    assert "Pending (2):" in text
    assert "simple_bgp/link_down" in text


def test_format_run_plan_lists_by_default_and_truncates() -> None:
    labels = [f"dc_clos/p{i} s t01" for i in range(1, 9)]
    text = format_run_plan(
        RunPlan(
            total_trials=8,
            pending_count=8,
            skipped_count=0,
            agent_type="mock",
            model="mock",
            result_dir="/tmp/r",
            pending_labels=labels,
        )
    )
    assert "8/8 run(s) remaining" in text
    assert "8 case(s) × 1 trial(s)/case" in text
    assert "Done: (none)" in text
    assert "Pending (8):" in text
    assert labels[0] in text
    multi = format_run_plan(
        RunPlan(
            total_trials=15,
            pending_count=15,
            skipped_count=0,
            agent_type="mock",
            model="mock",
            result_dir="/tmp/r",
            pending_labels=[],
            case_count=3,
            n_trials=5,
        )
    )
    assert "3 case(s) × 5 trial(s)/case" in multi
    assert "15/15 run(s) remaining" in multi
    assert "Pending: 15 (use -v to list)" in multi
    sample = format_run_plan(
        RunPlan(
            total_trials=8,
            pending_count=8,
            skipped_count=0,
            agent_type="mock",
            model="mock",
            result_dir="/tmp/r",
            pending_labels=labels,
        ),
        max_labels=3,
    )
    assert "Pending (8):" in sample
    assert "… and 5 more" in sample
    assert labels[0] in sample
    assert labels[3] not in sample
    full = format_run_plan(
        RunPlan(
            total_trials=8,
            pending_count=8,
            skipped_count=0,
            agent_type="mock",
            model="mock",
            result_dir="/tmp/r",
            pending_labels=labels,
        ),
        max_labels=10_000,
    )
    assert "… and" not in full
    assert labels[-1] in full


def test_format_trajectory_event_compacts_tools_and_assistant() -> None:
    from nika.workflows.benchmark.display import format_trajectory_event

    assert (
        format_trajectory_event(
            {
                "event": "tool_start",
                "tool": {"name": "ping_pair"},
                "input": '{"host_a": "client_0"}',
            }
        )
        == 'tool  ping_pair  {"host_a": "client_0"}'
    )
    assert format_trajectory_event({"event": "llm_start"}) == "llm  start"
    line = format_trajectory_event(
        {"event": "llm_end", "text": "Anomaly detected on eth0. " * 20}
    )
    assert line is not None
    assert line.startswith("assistant  ")
    assert line.endswith("…")
    assert format_trajectory_event({"event": "tool_end"}) is None


def test_messages_tail_polls_jsonl(tmp_path: Path) -> None:
    from nika.workflows.benchmark.display import _MessagesTail

    path = tmp_path / "messages.jsonl"
    tail = _MessagesTail(path=path)
    assert tail.poll() == []
    path.write_text(
        json.dumps(
            {
                "event": "tool_start",
                "tool": {"name": "exec_shell"},
                "input": "{}",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert tail.poll() == ["tool  exec_shell"]
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": "llm_end", "text": "done diagnosing"}) + "\n")
    assert tail.poll() == ["assistant  done diagnosing"]
    assert list(tail.lines)[-1] == "assistant  done diagnosing"


def test_messages_tail_keeps_incomplete_json_line(tmp_path: Path) -> None:
    from nika.workflows.benchmark.display import _MessagesTail

    path = tmp_path / "messages.jsonl"
    tail = _MessagesTail(path=path)
    path.write_text('{"event":"llm_start"', encoding="utf-8")
    assert tail.poll() == []
    assert tail.pending.startswith("{")
    with path.open("a", encoding="utf-8") as fh:
        fh.write("}\n")
    assert tail.poll() == ["llm  start"]


def test_benchmark_progress_dual_panel_render() -> None:
    from rich.console import Console
    from rich.layout import Layout

    with patch("nika.workflows.benchmark.display._console") as console:
        console.is_terminal = True
        progress = BenchmarkProgress(2, agent_type="mock", model="m")
        progress._use_live = True
        progress._term_size = (100, 32)
        progress._progress = __import__(
            "rich.progress", fromlist=["Progress"]
        ).Progress()
        progress._task_id = progress._progress.add_task("trials", total=2)
        progress.start_trials(["dc_clos/link_down m t01"])
        progress.set_phase("dc_clos/link_down m t01", "agent")
        renderable = progress._render()
        assert isinstance(renderable, Layout)
        buf = Console(record=True, width=100, height=32)
        buf.print(renderable)
        out = buf.export_text()
        assert "runs complete" in out
        assert "phase  agent" in out
        assert "dc_clos/link_down m t01" in out


def test_benchmark_progress_shows_inspect_url() -> None:
    from rich.console import Console
    from rich.style import Style

    url = "http://127.0.0.1:7580/"
    with patch("nika.workflows.benchmark.display._console") as console:
        console.is_terminal = True
        progress = BenchmarkProgress(
            2,
            agent_type="byo.langgraph",
            model="qwen",
            case_count=85,
            n_trials=3,
            inspect_url=url,
        )
        progress._use_live = True
        progress._term_size = (120, 32)
        progress._progress = __import__(
            "rich.progress", fromlist=["Progress"]
        ).Progress()
        progress._task_id = progress._progress.add_task("trials", total=2)
        panel = progress._job_panel()
        buf = Console(record=True, width=120, height=12, force_terminal=True)
        buf.print(panel)
        plain = buf.export_text()
        assert "agent=byo.langgraph" in plain
        assert "85 case(s)×3 trial(s)" in plain
        assert "inspect" in plain
        assert url in plain
        linked = [
            seg
            for seg in buf.render(panel)
            if seg.style and isinstance(seg.style, Style) and seg.style.link == url
        ]
        assert any(seg.text == "inspect" for seg in linked)


_RESIZE_CHILD = """
import json, sys, tempfile, time
from pathlib import Path
from nika.workflows.benchmark.display import BenchmarkProgress
root = Path(tempfile.mkdtemp())
with BenchmarkProgress(2, agent_type="mock") as progress:
    progress.start_trials(["dc_clos/link_down m t01"])
    progress.attach_session("dc_clos/link_down m t01", root)
    progress.set_phase("dc_clos/link_down m t01", "agent")
    for i in range(30):
        with (root / "messages.jsonl").open("a") as fh:
            fh.write(json.dumps({"event": "tool_start", "tool": {"name": "exec"}}) + "\\n")
        time.sleep(0.1)
    progress.finish_trial("dc_clos/link_down m t01")
print("CHILD_DONE")
"""


def test_dashboard_survives_drag_resize() -> None:
    """A burst of window resizes must not hang the run (real pty)."""
    pty = pytest.importorskip("pty")
    import fcntl
    import os
    import select
    import struct
    import termios

    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - child process
        # Live progress is disabled under pytest; the child is a real terminal run.
        env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
        os.execve(sys.executable, [sys.executable, "-c", _RESIZE_CHILD], env)

    def set_size(cols: int, rows: int) -> None:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    set_size(100, 30)
    output = b""
    start = time.monotonic()
    try:
        while time.monotonic() - start < 20:
            elapsed = time.monotonic() - start
            if 0.5 < elapsed < 2.5:
                step = int(elapsed * 100)
                set_size(70 + step % 50, 20 + step % 15)
            ready, _, _ = select.select([fd], [], [], 0.01)
            if not ready:
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
    finally:
        if os.waitpid(pid, os.WNOHANG) == (0, 0):
            os.kill(pid, 9)
            os.waitpid(pid, 0)
    assert b"CHILD_DONE" in output
    assert b"1/2 runs complete" in output


def test_confirm_run_skips_when_yes() -> None:
    with patch("nika.workflows.benchmark.display.typer.confirm") as confirm:
        assert confirm_run(yes=True) is True
        confirm.assert_not_called()


def test_confirm_run_skips_when_non_tty() -> None:
    with (
        patch("nika.workflows.benchmark.display.sys.stdin.isatty", return_value=False),
        patch("nika.workflows.benchmark.display.typer.confirm") as confirm,
    ):
        assert confirm_run(yes=False) is True
        confirm.assert_not_called()


def test_rolling_stats_req_rate_and_token_minmax() -> None:
    from nika.workflows.benchmark.display import _RollingStats

    stats = _RollingStats()
    stats.observe(
        {"steps": 10, "in_tokens": 100, "out_tokens": 20, "tool_calls": 3},
        failed=False,
    )
    stats.observe(
        {"steps": 30, "in_tokens": 300, "out_tokens": 40, "tool_calls": 5},
        failed=False,
    )
    line = stats.throughput_line(completed=2, elapsed_s=10.0)
    assert "0.20 trials/s" in line
    assert "4.00 req/s" in line  # (10+30)/10
    assert "avg tools=4.0" in line
    tokens = stats.token_line()
    assert "in_tok min/avg/max=100/200/300" in tokens
    assert "out_tok min/avg/max=20/30/40" in tokens


def test_benchmark_progress_non_tty_fallback(capsys) -> None:
    with patch("nika.workflows.benchmark.display._console") as console:
        console.is_terminal = False
        with BenchmarkProgress(2, initial_completed=0) as progress:
            progress.start_trials(["dc_clos/link_down m t01"])
            progress.finish_trial(
                "dc_clos/link_down m t01",
                metrics={
                    "in_tokens": 10,
                    "out_tokens": 4,
                    "tool_calls": 2,
                    "steps": 3,
                    "rca_f1": 1.0,
                    "localization_f1": 0.8,
                    "detection_score": 1.0,
                },
            )
    out = capsys.readouterr().out
    assert "→ start  dc_clos/link_down m t01" in out
    assert "✓ done  1/2  dc_clos/link_down m t01" in out
    assert "rca=1.000" in out
    assert "avg rca_f1=1.000" in out
    assert "in_tok min/avg/max=10/10/10" in out
    assert "Progress: 0/2" in out or "remaining" in out


def test_benchmark_progress_agent_mode_forces_plain_on_tty(capsys) -> None:
    """``--output-mode agent`` disables Live even when stdout is a TTY."""
    with patch("nika.workflows.benchmark.display._console") as console:
        console.is_terminal = True
        with BenchmarkProgress(1, output_mode="agent") as progress:
            assert progress._use_live is False
            progress.start_trials(["dc_clos/link_down m t01"])
            progress.set_phase("dc_clos/link_down m t01", "agent")
            progress.finish_trial(
                "dc_clos/link_down m t01",
                metrics={"rca_f1": 1.0},
            )
    out = capsys.readouterr().out
    assert "→ start  dc_clos/link_down m t01" in out
    assert "phase  agent  dc_clos/link_down m t01" in out
    assert "✓ done  1/1  dc_clos/link_down m t01" in out
    assert "\x1b[?1049h" not in out  # no alt-screen


def test_benchmark_progress_abandon_and_resync(capsys) -> None:
    with patch("nika.workflows.benchmark.display._console") as console:
        console.is_terminal = False
        with BenchmarkProgress(3, initial_completed=1) as progress:
            progress.start_trials(["a", "b"])
            progress.abandon_trial("a")
            assert progress.completed == 1
            progress.set_completed(2)
            assert progress.completed == 2
            progress.finish_trial("b", failed=True)
            assert progress.completed == 3
    out = capsys.readouterr().out
    assert "✗ fail  3/3  b" in out


def test_start_trials_merges_running_set() -> None:
    """Sliding-window concurrency adds to the active set instead of replacing it."""
    with patch("nika.workflows.benchmark.display._console") as console:
        console.is_terminal = False
        with BenchmarkProgress(3) as progress:
            progress.start_trials(["a"])
            progress.start_trials(["b"])
            assert progress._running == ["a", "b"]
            progress.finish_trial("a")
            assert progress._running == ["b"]


def test_read_trial_metrics(tmp_path: Path) -> None:
    (tmp_path / "eval_metrics.json").write_text(
        json.dumps({"in_tokens": 3, "out_tokens": 1, "tool_calls": 5}),
        encoding="utf-8",
    )
    assert read_trial_metrics(tmp_path)["tool_calls"] == 5
    assert read_trial_metrics(tmp_path / "missing") is None


def test_quiet_third_party_logging_raises_noisy_levels() -> None:
    import logging

    noisy = logging.getLogger("httpx")
    noisy.setLevel(logging.INFO)
    quiet_third_party_logging()
    assert noisy.level == logging.ERROR
    assert logging.getLogger().level >= logging.WARNING
    assert logging.getLogger("nika.run_config.loader").level == logging.ERROR
    assert logging.getLogger("langchain_core.callbacks.manager").level == logging.ERROR
    assert logging.getLogger("urllib3.connectionpool").level == logging.ERROR


def test_deferred_warnings_panel(capsys) -> None:
    import warnings

    from nika.cli import warning_capture as wc

    # Reset capture state for isolation.
    wc._installed = False
    wc._counts.clear()
    wc.install_warning_capture()
    # Bypass the default "once" registry so duplicates are counted.
    warnings.showwarning(
        UserWarning("lifespan incomplete definition demo"),
        UserWarning,
        __file__,
        1,
    )
    warnings.showwarning(
        UserWarning("some other actionable warning"),
        UserWarning,
        __file__,
        2,
    )
    # Known MCP/pydantic noise must not surface.
    try:
        from pydantic_settings.exceptions import (
            IncompleteFieldDefinitionWarning,
        )

        warnings.showwarning(
            IncompleteFieldDefinitionWarning(
                "Field 'lifespan' has an incomplete definition"
            ),
            IncompleteFieldDefinitionWarning,
            __file__,
            3,
        )
    except ImportError:
        pass
    wc.print_deferred_warnings()
    out = capsys.readouterr().out
    assert "Warnings" in out
    assert "some other actionable warning" in out
    assert "IncompleteFieldDefinitionWarning" not in out
    # Lifespan demo UserWarning also filtered by message heuristic.
    assert "lifespan incomplete definition demo" not in out
    # Second flush is a no-op.
    wc.print_deferred_warnings()
    assert capsys.readouterr().out == ""


def test_ensure_fastmcp_settings_ready_avoids_lifespan_warning() -> None:
    import warnings

    from nika.mcp.fastmcp_settings import ensure_fastmcp_settings_ready

    ensure_fastmcp_settings_ready()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        from mcp.server.fastmcp import FastMCP

        FastMCP("nika_settings_ready_test")
    # Match by message: pydantic_settings only ships
    # ``IncompleteFieldDefinitionWarning`` in some releases.
    assert not any("incomplete definition" in str(w.message) for w in caught)


def test_scan_trials_quiet_collapses_skips(tmp_path: Path, capsys) -> None:
    row_a = {
        "scenario": "simple_bgp",
        "problem": "link_down",
        "topo_size": "",
        "inject": {"src": "r1", "dst": "r2"},
    }
    row_b = {**row_a, "problem": "link_flap"}
    trials = expand_trials([row_a, row_b], n_trials=1)
    write_valid_trial(
        trial_dir(tmp_path, trials[0].case_key, trials[0].trial_index),
        outcome="success",
        session_id=trials[0].trial_id,
    )

    _root, pending = scan_trials(
        trials=trials, result_dir=tmp_path, resume=True, verbose=False
    )
    out = capsys.readouterr().out
    assert pending == [1]
    assert "Resuming run: 1/2" in out
    assert "skip (already complete" not in out

    _root, pending = scan_trials(
        trials=trials, result_dir=tmp_path, resume=True, verbose=True
    )
    out = capsys.readouterr().out
    assert pending == [1]
    assert "skip (already complete" in out
