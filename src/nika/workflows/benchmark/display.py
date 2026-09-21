"""Human-facing console output for ``nika benchmark run``.

Inspect-style Rich Live dashboard:

- fixed top **job** panel (progress / scores) — height does not grow
- one **session** panel per running trial — fixed height, trajectory
  scrolls inside the box (newest lines at the bottom)
- ``Layout`` keeps the overall footprint stable to reduce full-screen flicker
- tick loop refreshes spinner/time and tails ``messages.jsonl``
- final frame is printed to the main screen after Live exits
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

import typer
from rich.console import Console, RenderableType
from rich.control import Control
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

from nika.evaluator.result_log import MESSAGES_FILENAME

# force_interactive: Live only cursor-ups/erases prior frames when interactive.
_console = Console(force_interactive=True)

_RECENT_KEEP = 2
_TRAJ_KEEP = 8
_THROTTLE_S = 0.5
_TICK_S = 0.5
# Default preflight list length (Done + Pending). ``-v`` uses a large cap.
_DEFAULT_PLAN_LABELS = 24
_TRAJ_TRUNCATE = 72
# Fixed dashboard chrome (rows). Session boxes share the remainder.
_JOB_LAYOUT_ROWS = 9
_SESSION_MIN_ROWS = 7


def _read_terminal_size() -> tuple[int, int] | None:
    """Return current ``(columns, lines)`` or None if unavailable."""
    for fd in (1, 0, 2):
        try:
            size = os.get_terminal_size(fd)
        except OSError:
            continue
        if size.columns > 0 and size.lines > 0:
            return (size.columns, size.lines)
    return None


def install_warning_capture() -> None:
    from nika.cli.warning_capture import install_warning_capture as _install

    _install()


def quiet_third_party_logging() -> None:
    """Silence noisy loggers / library warnings for Live-safe runs."""
    from nika.cli.warning_capture import (
        ignore_known_library_warnings,
        quiet_noisy_loggers,
        warning_capture_installed,
    )

    quiet_noisy_loggers()
    if not warning_capture_installed():
        ignore_known_library_warnings()


def print_deferred_warnings() -> None:
    from nika.cli.warning_capture import print_deferred_warnings as _print

    _print()


def apply_worker_warning_env() -> None:
    from nika.cli.warning_capture import apply_worker_warning_env as _apply

    _apply()


@dataclass(frozen=True)
class RunPlan:
    """Preflight summary shown before a batch/release run starts."""

    total_trials: int
    pending_count: int
    skipped_count: int
    agent_type: str
    model: str | None
    result_dir: str
    pending_labels: Sequence[str]
    skipped_labels: Sequence[str] = ()
    header: str | None = None
    batch_size: int = 1
    case_count: int | None = None
    n_trials: int = 1


def _append_label_section(
    lines: list[str],
    *,
    title: str,
    labels: Sequence[str],
    count: int,
    max_labels: int,
    empty: str,
) -> None:
    if count <= 0 and not labels:
        lines.append(f"{title}: {empty}")
        return
    limit = max(0, int(max_labels))
    shown = list(labels[:limit]) if limit > 0 else []
    extra = max(0, count - len(shown))
    if not shown and count > 0:
        lines.append(f"{title}: {count} (use -v to list)")
        return
    lines.append(f"{title} ({count}):")
    for label in shown:
        lines.append(f"  - {label}")
    if extra > 0:
        lines.append(f"  … and {extra} more")


def format_run_plan(plan: RunPlan, *, max_labels: int = _DEFAULT_PLAN_LABELS) -> str:
    """Return a multi-line plan string (no trailing newline)."""
    lines: list[str] = []
    if plan.header:
        lines.append(plan.header)
    case_count = (
        plan.case_count
        if plan.case_count is not None
        else max(1, plan.total_trials // max(1, plan.n_trials))
    )
    n_trials = max(1, int(plan.n_trials))
    lines.append(
        f"Plan: {plan.pending_count}/{plan.total_trials} run(s) remaining "
        f"({plan.skipped_count} already complete), "
        f"{case_count} case(s) × {n_trials} trial(s)/case, "
        f"batch_size={plan.batch_size}"
    )
    lines.append(
        f"Agent: {plan.agent_type}"
        + (f"  model={plan.model}" if plan.model else "")
    )
    lines.append(f"Results: {plan.result_dir}")
    _append_label_section(
        lines,
        title="Done",
        labels=plan.skipped_labels,
        count=plan.skipped_count,
        max_labels=max_labels,
        empty="(none)",
    )
    _append_label_section(
        lines,
        title="Pending",
        labels=plan.pending_labels,
        count=plan.pending_count,
        max_labels=max_labels,
        empty="(none)",
    )
    return "\n".join(lines)


def print_run_plan(
    plan: RunPlan, *, max_labels: int = _DEFAULT_PLAN_LABELS
) -> None:
    _console.print(format_run_plan(plan, max_labels=max_labels))


def confirm_run(*, yes: bool) -> bool:
    """Return True if the run should proceed.

    Skips the prompt when ``yes`` is set or stdin is not a TTY (CI / pipes).
    """
    if yes or not sys.stdin.isatty():
        return True
    return bool(typer.confirm("Proceed with this benchmark run?", default=True))


def print_inspect_hint(result_dir: str | Any) -> None:
    _console.print(
        f"Browse trajectories: nika inspect --result-dir {result_dir}"
    )


def vprint(verbose: bool, message: str) -> None:
    """Print only in verbose mode."""
    if verbose:
        print(message)


def _fmt_score(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.3f}"


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _truncate(text: str, limit: int = _TRAJ_TRUNCATE) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def format_trajectory_event(event: dict[str, Any]) -> str | None:
    """Compact one ``messages.jsonl`` event for the session panel."""
    kind = str(event.get("event") or "")
    if kind == "tool_start":
        tool = event.get("tool") or {}
        name = tool.get("name") if isinstance(tool, dict) else None
        name = str(name or "tool")
        raw_input = event.get("input") or ""
        if isinstance(raw_input, (dict, list)):
            raw_input = json.dumps(raw_input, ensure_ascii=False, default=str)
        detail = (
            _truncate(str(raw_input))
            if str(raw_input).strip() not in ("", "{}")
            else ""
        )
        return f"tool  {name}" + (f"  {detail}" if detail else "")
    if kind == "llm_start":
        return "llm  start"
    if kind == "llm_end":
        text = event.get("text") or event.get("content") or ""
        if not text:
            return "llm  end"
        return f"assistant  {_truncate(str(text))}"
    if kind == "diagnosis_frozen":
        return "event  diagnosis_frozen"
    return None


@dataclass
class _MessagesTail:
    """Byte-offset tail of ``messages.jsonl`` for one trial session."""

    path: Path
    offset: int = 0
    pending: str = ""
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=_TRAJ_KEEP))

    def poll(self) -> list[str]:
        """Read new JSONL events; return newly formatted lines."""
        if not self.path.is_file():
            return []
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.offset:
            self.offset = 0
            self.pending = ""
        if size == self.offset and not self.pending:
            return []
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                fh.seek(self.offset)
                chunk = fh.read()
                self.offset = fh.tell()
        except OSError:
            return []
        data = self.pending + chunk
        self.pending = ""
        new_lines: list[str] = []
        parts = data.split("\n")
        if data and not data.endswith("\n"):
            self.pending = parts.pop()
        for raw in parts:
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            line = format_trajectory_event(event)
            if line:
                self.lines.append(line)
                new_lines.append(line)
        return new_lines


@dataclass
class _SessionState:
    label: str
    phase: str = "pending"
    started: float = field(default_factory=time.monotonic)
    session_dir: Path | None = None
    tail: _MessagesTail | None = None


@dataclass
class _RollingStats:
    rca_f1: list[float] = field(default_factory=list)
    localization_f1: list[float] = field(default_factory=list)
    detection_score: list[float] = field(default_factory=list)
    in_tokens: list[int] = field(default_factory=list)
    out_tokens: list[int] = field(default_factory=list)
    tool_calls: list[int] = field(default_factory=list)
    steps: list[int] = field(default_factory=list)
    successes: int = 0
    failures: int = 0

    def observe(self, metrics: dict[str, Any] | None, *, failed: bool) -> None:
        if failed:
            self.failures += 1
        else:
            self.successes += 1
        if not metrics:
            return
        for key, bucket in (
            ("rca_f1", self.rca_f1),
            ("localization_f1", self.localization_f1),
            ("detection_score", self.detection_score),
        ):
            value = metrics.get(key)
            if isinstance(value, (int, float)) and float(value) >= 0:
                bucket.append(float(value))
        for key, bucket_i in (
            ("in_tokens", self.in_tokens),
            ("out_tokens", self.out_tokens),
            ("tool_calls", self.tool_calls),
            ("steps", self.steps),
        ):
            value = metrics.get(key)
            if isinstance(value, (int, float)) and value >= 0:
                bucket_i.append(int(value))

    def score_line(self) -> str:
        return (
            f"avg rca_f1={_fmt_score(_mean(self.rca_f1))}  "
            f"loc_f1={_fmt_score(_mean(self.localization_f1))}  "
            f"det={_fmt_score(_mean(self.detection_score))}  "
            f"ok={self.successes}  fail={self.failures}"
        )

    def throughput_line(self, *, completed: int, elapsed_s: float) -> str:
        parts: list[str] = []
        if elapsed_s > 0 and completed > 0:
            parts.append(f"{completed / elapsed_s:.2f} trials/s")
        if elapsed_s > 0 and self.steps:
            parts.append(f"{sum(self.steps) / elapsed_s:.2f} req/s")
        if self.tool_calls:
            parts.append(
                f"avg tools={sum(self.tool_calls) / len(self.tool_calls):.1f}"
            )
        return "  ".join(parts) if parts else "—"

    def token_line(self) -> str:
        parts: list[str] = []
        for label, values in (
            ("in_tok", self.in_tokens),
            ("out_tok", self.out_tokens),
        ):
            stats = _minmax_avg(values)
            if stats is None:
                continue
            lo, avg, hi = stats
            parts.append(f"{label} min/avg/max={lo}/{avg:.0f}/{hi}")
        return "  ".join(parts) if parts else "—"


def _minmax_avg(values: list[int]) -> tuple[int, float, int] | None:
    if not values:
        return None
    return min(values), sum(values) / len(values), max(values)


def _short_score_suffix(metrics: dict[str, Any] | None) -> str:
    if not metrics:
        return ""
    bits: list[str] = []
    for key, label in (
        ("rca_f1", "rca"),
        ("localization_f1", "loc"),
        ("detection_score", "det"),
    ):
        value = metrics.get(key)
        if isinstance(value, (int, float)) and float(value) >= 0:
            bits.append(f"{label}={float(value):.3f}")
    return ("  " + " ".join(bits)) if bits else ""


def _fmt_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


class BenchmarkProgress:
    """Inspect-style Rich Live: job panel + current-session panel."""

    def __init__(
        self,
        total: int,
        *,
        initial_completed: int = 0,
        agent_type: str | None = None,
        model: str | None = None,
        case_count: int | None = None,
        n_trials: int = 1,
    ) -> None:
        self.total = max(0, int(total))
        self.completed = max(0, int(initial_completed))
        self._agent_type = agent_type
        self._model = model
        self._case_count = case_count
        self._n_trials = max(1, int(n_trials))
        self._stats = _RollingStats()
        self._started = time.monotonic()
        self._sessions: dict[str, _SessionState] = {}
        self._running: list[str] = []
        self._recent: deque[str] = deque(maxlen=_RECENT_KEEP)
        self._traj_feed: deque[str] = deque(maxlen=_TRAJ_KEEP)
        self._progress: Progress | None = None
        self._task_id: TaskID | None = None
        self._live: Live | None = None
        self._use_live = _console.is_terminal and self.total > 0
        self._lock = threading.Lock()
        self._last_refresh = 0.0
        self._dirty = False
        self._ticker_stop = threading.Event()
        self._ticker: threading.Thread | None = None
        self._term_size: tuple[int, int] | None = None
        self._prev_sigwinch: Any = None
        self._resize_pending = False
        self._resize_wake = threading.Event()

    def __enter__(self) -> Self:
        quiet_third_party_logging()
        if self._use_live:
            self._progress = Progress(
                SpinnerColumn(),
                TextColumn("{task.description}"),
                # bar_width=None → flex with terminal width on resize
                BarColumn(bar_width=None),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                console=_console,
                transient=True,
                expand=True,
            )
            self._task_id = self._progress.add_task(
                "runs",
                total=self.total,
                completed=self.completed,
            )
            self._live = Live(
                self._render(),
                console=_console,
                screen=True,
                auto_refresh=False,
                redirect_stdout=True,
                redirect_stderr=True,
                # Crop (not ellipsis) so a fixed Layout footprint stays stable.
                vertical_overflow="crop",
            )
            self._live.start()
            quiet_third_party_logging()
            self._term_size = _read_terminal_size()
            self._install_resize_handler()
            self._refresh(force=True)
            self._ticker_stop.clear()
            self._ticker = threading.Thread(
                target=self._tick_loop,
                name="nika-benchmark-progress-tick",
                daemon=True,
            )
            self._ticker.start()
        elif self.total > 0:
            print(
                f"Progress: {self.completed}/{self.total} complete "
                f"({self.total - self.completed} remaining)"
            )
            print(self._stats.score_line())
        return self

    def __exit__(self, *exc: object) -> None:
        self._restore_resize_handler()
        self._ticker_stop.set()
        self._resize_wake.set()
        if self._ticker is not None:
            self._ticker.join(timeout=0.5)
            self._ticker = None
        interrupted = bool(exc and exc[0] is not None and issubclass(
            exc[0], KeyboardInterrupt  # type: ignore[arg-type]
        ))
        final: RenderableType | None = None
        if self._live is not None:
            if not interrupted:
                try:
                    with self._lock:
                        if self._progress is not None and self._task_id is not None:
                            self._poll_trajectories()
                            self._progress.update(
                                self._task_id, completed=self.completed
                            )
                            final = self._render()
                            self._live.update(final, refresh=True)
                except Exception:  # noqa: BLE001 - teardown must not block Ctrl+C
                    final = None
            try:
                self._live.stop()
            except Exception:  # noqa: BLE001
                pass
            self._live = None
            if final is not None and not interrupted:
                try:
                    _console.print(final)
                except Exception:  # noqa: BLE001
                    pass
        self._progress = None
        self._task_id = None

    def _install_resize_handler(self) -> None:
        sig = getattr(signal, "SIGWINCH", None)
        if sig is None:
            return

        def _on_winch(_signum: int, _frame: Any) -> None:
            # Tick loop picks this up (avoid locking / I/O in the handler).
            self._resize_pending = True
            self._resize_wake.set()

        try:
            self._prev_sigwinch = signal.signal(sig, _on_winch)
        except (ValueError, OSError):
            # Not in main thread / unsupported — tick loop still polls size.
            self._prev_sigwinch = None

    def _restore_resize_handler(self) -> None:
        sig = getattr(signal, "SIGWINCH", None)
        if sig is None or self._prev_sigwinch is None:
            self._prev_sigwinch = None
            return
        try:
            signal.signal(sig, self._prev_sigwinch)
        except (ValueError, OSError):
            pass
        self._prev_sigwinch = None

    def _prepare_live_for_resize(self) -> None:
        """Clear only when width changes (reflow); height-only skips clear."""
        if self._live is None:
            return
        try:
            self._live.console.control(Control.clear())
        except Exception:  # noqa: BLE001 - best-effort terminal clear
            pass
        live_render = getattr(self._live, "_live_render", None)
        if live_render is not None:
            live_render._shape = None

    def _sync_terminal_size(self) -> bool:
        """Detect terminal resize; clear only on column (width) changes."""
        size = _read_terminal_size()
        if size is None:
            return False
        prev = self._term_size
        resized = prev is not None and prev != size
        if resized:
            width_changed = prev is not None and prev[0] != size[0]
            self._term_size = size
            self._resize_pending = False
            if width_changed:
                self._prepare_live_for_resize()
            return True
        if self._resize_pending:
            self._resize_pending = False
            if prev is None:
                self._term_size = size
            return True
        if prev is None:
            self._term_size = size
        return False

    def _dashboard_rows(self) -> int:
        size = self._term_size or _read_terminal_size() or (80, 28)
        return max(16, size[1] - 1)

    def _session_box_rows(self) -> int:
        """Fixed row budget per session panel (including borders)."""
        rows = self._dashboard_rows()
        n = max(1, len(self._running) or 1)
        avail = max(_SESSION_MIN_ROWS, rows - _JOB_LAYOUT_ROWS)
        return max(_SESSION_MIN_ROWS, avail // n)

    def _session_traj_rows(self, box_rows: int) -> int:
        # borders(2) + phase(1) → remaining lines scroll trajectory
        return max(3, min(_TRAJ_KEEP, box_rows - 3))

    def _tick_loop(self) -> None:
        while not self._ticker_stop.is_set():
            self._resize_wake.wait(timeout=_TICK_S)
            self._resize_wake.clear()
            if self._ticker_stop.is_set():
                break
            try:
                self._refresh(force=True)
            except Exception:  # noqa: BLE001 - tick must not kill the run
                return

    def start_trials(self, labels: Sequence[str]) -> None:
        with self._lock:
            self._running = list(labels)
            now = time.monotonic()
            for label in labels:
                state = self._sessions.get(label)
                if state is None:
                    state = _SessionState(label=label, phase="running", started=now)
                    self._sessions[label] = state
                else:
                    state.phase = state.phase or "running"
                    state.started = now
                if not self._use_live:
                    print(f"→ start  {label}")
        self._refresh(force=True)

    def attach_session(self, label: str, session_dir: str | Path) -> None:
        path = Path(session_dir)
        with self._lock:
            state = self._sessions.get(label)
            if state is None:
                state = _SessionState(label=label, phase="deploy")
                self._sessions[label] = state
                if label not in self._running:
                    self._running.append(label)
            state.session_dir = path
            state.tail = _MessagesTail(path=path / MESSAGES_FILENAME)
        self._refresh(force=True)

    def set_phase(self, label: str, phase: str) -> None:
        with self._lock:
            state = self._sessions.get(label)
            if state is None:
                state = _SessionState(label=label, phase=phase)
                self._sessions[label] = state
                if label not in self._running:
                    self._running.append(label)
            else:
                state.phase = phase
            if not self._use_live:
                print(f"  phase  {phase}  {label}")
        self._refresh(force=True)

    def abandon_trial(self, label: str) -> None:
        """Drop a running trial from the UI without advancing the counter.

        Used when a trial errors but leaves an incomplete slot (retryable).
        """
        with self._lock:
            if label in self._running:
                self._running.remove(label)
            self._sessions.pop(label, None)
        self._refresh(force=True)

    def set_completed(self, completed: int) -> None:
        """Resync the progress counter from on-disk counted trials (e.g. retries)."""
        with self._lock:
            self.completed = min(self.total, max(0, int(completed)))
        self._refresh(force=True)

    def finish_trial(
        self,
        label: str,
        *,
        metrics: dict[str, Any] | None = None,
        failed: bool = False,
    ) -> None:
        with self._lock:
            if label in self._running:
                self._running.remove(label)
            state = self._sessions.pop(label, None)
            if state is not None and state.tail is not None:
                state.tail.poll()
                for line in state.tail.lines:
                    self._traj_feed.append(line)
            self._stats.observe(metrics, failed=failed)
            self.completed = min(self.total, self.completed + 1)
            mark = "✗ fail" if failed else "✓ done"
            line = (
                f"{mark}  {self.completed}/{self.total}  {label}"
                + _short_score_suffix(metrics)
            )
            self._recent.append(line)
            if not self._use_live:
                print(line)
                print(f"  {self._stats.score_line()}")
                elapsed = max(0.0, time.monotonic() - self._started)
                print(
                    "  "
                    + self._stats.throughput_line(
                        completed=self.completed, elapsed_s=elapsed
                    )
                )
                print(f"  {self._stats.token_line()}")
        self._refresh(force=True)

    def log(self, message: str) -> None:
        with self._lock:
            self._recent.append(message)
            if not self._use_live:
                print(message)
        self._refresh(force=True)

    def _poll_trajectories(self) -> None:
        for label in list(self._running):
            state = self._sessions.get(label)
            if state is not None and state.tail is not None:
                for line in state.tail.poll():
                    self._traj_feed.append(line)

    def _refresh(self, *, force: bool = False) -> None:
        if self._live is None or not self._live.is_started:
            return
        if self._progress is None or self._task_id is None:
            return
        now = time.monotonic()
        resized = self._sync_terminal_size()
        if not force and not resized and (now - self._last_refresh) < _THROTTLE_S:
            self._dirty = True
            return
        self._last_refresh = now
        self._dirty = False
        with self._lock:
            self._poll_trajectories()
            self._progress.update(self._task_id, completed=self.completed)
            renderable = self._render()
        self._live.update(renderable, refresh=True)

    def _job_panel(self) -> Panel:
        assert self._progress is not None
        elapsed = max(0.0, time.monotonic() - self._started)
        subtitle = Table.grid(expand=True)
        subtitle.add_column()
        subtitle.add_column(justify="right")
        left = "benchmark"
        if self._agent_type:
            left = f"agent={self._agent_type}"
            if self._model:
                left += f"  model={self._model}"
        case_count = self._case_count
        if case_count is None and self._n_trials > 0:
            case_count = max(1, self.total // self._n_trials) if self.total else 0
        if case_count is not None:
            left += f"  {case_count} case(s)×{self._n_trials} trial(s)"
        right = self._stats.throughput_line(
            completed=self.completed, elapsed_s=elapsed
        )
        subtitle.add_row(left, right)

        body = Table.grid(expand=True)
        body.add_column()
        body.add_row(subtitle)
        body.add_row(self._progress)
        body.add_row(Text(self._stats.score_line()))
        body.add_row(Text(self._stats.token_line(), style="dim"))
        if self._recent:
            body.add_row(
                Text(" · ".join(list(self._recent)[-_RECENT_KEEP:]), style="dim")
            )

        title = f"{self.completed}/{self.total} runs complete"
        return Panel(
            body,
            title=f"[bold]{title}[/bold]",
            title_align="left",
            expand=True,
            border_style="blue",
        )

    def _session_scroll_panel(self, label: str, *, box_rows: int) -> Panel:
        """Fixed-height session box; trajectory scrolls inside (newest at bottom)."""
        now = time.monotonic()
        state = self._sessions.get(label)
        phase = state.phase if state else "running"
        started = state.started if state else now
        traj_rows = self._session_traj_rows(box_rows)

        header = f"phase  {phase} · {_fmt_elapsed(now - started)}"
        traj = list(state.tail.lines) if state and state.tail else []
        if not traj and self._traj_feed and not self._running:
            traj = list(self._traj_feed)
        shown = traj[-traj_rows:]
        # Pad above so the box height never changes as lines arrive.
        pad = traj_rows - len(shown)
        scroll_lines = [""] * max(0, pad) + shown

        body = Table.grid(expand=True)
        body.add_column(no_wrap=True, overflow="ellipsis")
        body.add_row(Text(header, style="bold"))
        for line in scroll_lines:
            body.add_row(Text(f"  {line}" if line else " ", style="dim" if not line else ""))

        title = label if len(label) <= 64 else label[:63] + "…"
        return Panel(
            body,
            title=f"[bold]{title}[/bold]",
            title_align="left",
            expand=True,
            border_style="cyan",
            height=box_rows,
        )

    def _sessions_layout(self) -> Layout:
        region = Layout(name="sessions")
        box_rows = self._session_box_rows()
        if not self._running:
            region.update(
                Panel(
                    Text("— idle —", style="dim"),
                    title="[bold]Sessions[/bold]",
                    title_align="left",
                    border_style="cyan",
                    height=box_rows,
                )
            )
            return region

        children = [
            Layout(
                self._session_scroll_panel(label, box_rows=box_rows),
                name=f"session-{index}",
                size=box_rows,
            )
            for index, label in enumerate(self._running)
        ]
        region.split_column(*children)
        return region

    def _render(self) -> RenderableType:
        assert self._progress is not None
        root = Layout(name="root")
        root.split_column(
            Layout(self._job_panel(), name="job", size=_JOB_LAYOUT_ROWS),
            self._sessions_layout(),
        )
        return root


def read_trial_metrics(session_dir: Any) -> dict[str, Any] | None:
    """Best-effort load of ``eval_metrics.json`` from a trial directory."""
    path = Path(session_dir) / "eval_metrics.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
