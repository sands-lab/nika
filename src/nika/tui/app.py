"""Interactive NIKA shell: slash commands and guided forms over the ``nika`` CLI.

Every action runs the regular CLI in a subprocess and echoes the equivalent
``nika ...`` command, so the TUI never diverges from scripted usage.
"""

from __future__ import annotations

import difflib
import os
import shlex
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit import prompt as text_prompt
from prompt_toolkit.completion import Completer, Completion, ThreadedCompleter
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.styles import Style
from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from agent.utils.endpoint_probe import (
    AUTH_FAILED,
    MODEL_NOT_LISTED,
    NO_CREDENTIALS,
    OK,
    ProbeResult,
    probe_endpoint,
)
from nika.cli.prompts import choose
from nika.cli.utils import env_id_from_lab
from nika.run_config.loader import ENV_RUN_CONFIG, resolve_run_config_path
from nika.tui import art
from nika.tui.data import (
    AgentProfile,
    Task,
    TaskCatalog,
    list_profiles,
    load_profile,
    releases,
    running_sessions,
    session_hosts,
)

_UNSET = object()
_HIDDEN_PARAMS = {"help", "install_completion", "show_completion"}
_SIZES = [("s", "s  small"), ("m", "m  medium"), ("l", "l  large")]
_ACCENT = "#5fafff"
_EXIT_WINDOW_SEC = 2.0

_PT_STYLE = Style.from_dict(
    {
        "prompt": f"bold {_ACCENT}",
        "arrow": "#af5fff bold",
        "host": "bold ansigreen",
        "dim": "#808080",
        # Toolbar and completion menu follow the terminal theme (light or dark).
        "bottom-toolbar": "noreverse bg:default default",
        "tb.key": "#808080",
        "tb.val": "",
        "tb.ok": "ansigreen",
        "tb.warn": "ansiyellow",
        "tb.bad": "ansired",
        "tb.alert": "bold",
        "completion-menu": "bg:default default",
        "completion-menu.completion": "bg:default default",
        "completion-menu.completion.current": "bg:default default reverse",
        "completion-menu.meta.completion": "bg:default #808080",
        "completion-menu.meta.completion.current": "bg:default default reverse",
        "completion-menu.multi-column-meta": "bg:default #808080",
        "scrollbar.background": "bg:default default",
        "scrollbar.button": "bg:default default reverse",
        "scrollbar.arrow": "bg:default default",
    }
)


def probe_badge(result: ProbeResult | None) -> tuple[str, str]:
    """Short status text and rich style for an endpoint probe."""
    if result is None:
        return "… checking", "grey50"
    latency = f" {result.latency_ms:.0f}ms" if result.latency_ms else ""
    match result.status:
        case "ok":
            return f"● up{latency}", "green"
        case "model_not_listed":
            return f"● up{latency}, model not listed", "yellow"
        case "no_credentials":
            return "○ no credentials", "yellow"
        case "auth_failed":
            return "✗ auth rejected", "red"
        case "unreachable":
            return "✗ unreachable", "red"
    return "✗ error", "red"


@dataclass
class SlashCommand:
    name: str
    help: str
    handler: Callable[[list[str]], None]
    usage: str = ""
    group: str = "Guided"


@dataclass
class Form:
    """Values for one CLI command, rendered back into argv."""

    path: list[str]
    command: Any
    values: dict[str, Any] = field(default_factory=dict)

    def params(self) -> list[Any]:
        params = [
            p
            for p in self.command.params
            if p.name not in _HIDDEN_PARAMS and not getattr(p, "hidden", False)
        ]
        # Required fields first, otherwise declaration order.
        return sorted(params, key=lambda p: not p.required)

    def missing(self) -> list[Any]:
        return [
            p for p in self.params() if p.required and _empty(self.values.get(p.name))
        ]

    def argv(self) -> list[str]:
        options: list[str] = []
        arguments: list[str] = []
        for param in self.command.params:
            value = self.values.get(param.name)
            if _empty(value):
                continue
            if param.param_type_name == "argument":
                arguments.extend(value if isinstance(value, list) else [str(value)])
                continue
            flag = _long_opt(param.opts)
            if param.is_flag:
                if param.secondary_opts:
                    options.append(flag if value else _long_opt(param.secondary_opts))
                elif value:
                    options.append(flag)
            elif param.multiple:
                for item in value:
                    options.extend([flag, str(item)])
            else:
                options.extend([flag, str(value)])
        # Options first: some commands (exec) stop option parsing at arguments.
        return [*self.path, *options, *arguments]


def _empty(value: Any) -> bool:
    return value is None or value == [] or value == ""


def _long_opt(opts: list[str]) -> str:
    return max(opts, key=len)


def _param_label(param: Any) -> str:
    if param.param_type_name == "argument":
        return param.human_readable_name.upper()
    return _long_opt(param.opts)


def _format_value(value: Any) -> str:
    if _empty(value):
        return "·"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    return str(value)


class SlashCompleter(Completer):
    def __init__(self, tui: "NikaTui") -> None:
        self.tui = tui

    def get_completions(self, document, complete_event) -> Iterable[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        parts = text[1:].split(" ")
        word = parts[-1]
        if len(parts) == 1:
            for name, cmd in self.tui.commands.items():
                if name.startswith(word):
                    yield Completion(
                        f"/{name}",
                        start_position=-len(text),
                        display=f"/{name}",
                        display_meta=cmd.help,
                    )
            return
        name = parts[0]
        if name in ("run", "tasks") and len(parts) == 2 and self.tui.tasks.ready:
            matches = [t for t in self.tui.tasks.wait() if word in t.label]
            for task in matches[:200]:
                yield Completion(
                    task.label,
                    start_position=-len(word),
                    display_meta=task.domain or task.problem,
                )
            return
        if name == "use" and len(parts) == 2:
            for profile in list_profiles():
                if profile.display_path.startswith(word) or profile.name.startswith(
                    word
                ):
                    yield Completion(
                        profile.display_path,
                        start_position=-len(word),
                        display_meta=f"{profile.agent_type} · {profile.provider}/{profile.model}",
                    )
            return
        if name not in self.tui.cli_groups:
            return
        command = self.tui.cli_command([name])
        tokens = [p for p in parts[1:-1] if p]
        if command is not None and hasattr(command, "list_commands") and not tokens:
            for sub in self.tui.subcommands(command):
                if sub.startswith(word):
                    help_text = self.tui.cli_command([name, sub])
                    yield Completion(
                        sub,
                        start_position=-len(word),
                        display_meta=_short_help(help_text),
                    )
            return
        if word.startswith("-"):
            leaf = self.tui.cli_command([name, *tokens[:1]]) if tokens else command
            if leaf is None or hasattr(leaf, "list_commands"):
                return
            for param in leaf.params:
                if param.param_type_name != "option" or param.name in _HIDDEN_PARAMS:
                    continue
                for opt in [*param.opts, *param.secondary_opts]:
                    if opt.startswith(word) and opt.startswith("--"):
                        yield Completion(
                            opt,
                            start_position=-len(word),
                            display_meta=(param.help or "")[:80],
                        )


def _context(command: Any) -> Any:
    # context_class keeps this working whether typer uses click or its vendored copy.
    return command.context_class(command, info_name="nika")


def _short_help(command: Any) -> str:
    if command is None:
        return ""
    try:
        return command.get_short_help_str(80)
    except Exception:
        return ""


class NikaTui:
    def __init__(self, run_config: str | None = None) -> None:
        self.console = Console(highlight=False)
        self.tasks = TaskCatalog()
        self.probes: dict[Path, ProbeResult | None] = {}
        self.sessions: list[dict[str, Any]] | None = None
        self.profile: AgentProfile | None = None
        self._root: Any = None
        self._cli_cache: dict[tuple[str, ...], Any] = {}
        self._cli_lock = threading.Lock()
        self._warned_tasks = False
        self._exit_armed_until = 0.0
        self.cli_groups = sorted(_lazy_cli_names())
        self.commands = self._build_commands()
        self._select_profile(resolve_run_config_path(run_config))

    # ------------------------------------------------------------------ setup

    def _build_commands(self) -> dict[str, SlashCommand]:
        guided = [
            SlashCommand(
                "run",
                "Run an agent on a benchmark task (guided)",
                self.cmd_run,
                "[TASK_LABEL]",
            ),
            SlashCommand(
                "tasks", "Browse and search benchmark tasks", self.cmd_tasks, "[FILTER]"
            ),
            SlashCommand(
                "agents", "Agent profiles with live endpoint status", self.cmd_agents
            ),
            SlashCommand(
                "use", "Switch the active agent profile", self.cmd_use, "[RUN_CONFIG]"
            ),
            SlashCommand(
                "check",
                "Check that the active endpoint and model are up",
                self.cmd_check,
            ),
            SlashCommand(
                "sessions",
                "Running sessions and what you can do with them",
                self.cmd_sessions,
            ),
            SlashCommand(
                "lab",
                "Deploy a network scenario without a fault",
                self.cmd_lab,
                "[SCENARIO]",
            ),
            SlashCommand(
                "inject", "Inject a failure into a running session", self.cmd_inject
            ),
            SlashCommand(
                "shell", "Interactive shell on a lab host (nika exec)", self.cmd_shell
            ),
            SlashCommand(
                "bench", "Run or list frozen benchmark releases", self.cmd_bench
            ),
        ]
        cli = [
            SlashCommand(
                name, _LAZY_HELP[name], self.cli_handler(name), "[ARGS]", "CLI"
            )
            for name in self.cli_groups
        ]
        meta = [
            SlashCommand(
                "home",
                "Show the home screen",
                lambda _: self.home(animate=True),
                group="TUI",
            ),
            SlashCommand(
                "help",
                "List commands, or `/help CMD` for CLI help",
                self.cmd_help,
                "[CMD]",
                "TUI",
            ),
            SlashCommand(
                "clear", "Clear the screen", lambda _: self.console.clear(), group="TUI"
            ),
            SlashCommand("quit", "Leave the NIKA shell", self.cmd_quit, group="TUI"),
        ]
        return {c.name: c for c in [*guided, *cli, *meta]}

    def _select_profile(self, path: Path) -> None:
        if not path.is_file():
            # No config/nika.yaml yet: start from the first local copy or template.
            fallback = next(iter(list_profiles()), None)
            if fallback is None:
                return
            self.console.print(
                f"[yellow]{escape(str(path))} not found; using "
                f"{escape(fallback.display_path)}. Switch with /agents.[/]"
            )
            path = fallback.path
        try:
            self.profile = load_profile(path)
        except Exception as exc:
            self.profile = None
            self.console.print(
                f"[red]Could not load {escape(str(path))}: {escape(str(exc))}[/]"
            )
            return
        os.environ[ENV_RUN_CONFIG] = str(path)
        self.probe_async(self.profile)

    # --------------------------------------------------------------- probing

    def probe_async(self, profile: AgentProfile) -> threading.Thread:
        self.probes[profile.path] = None

        def work() -> None:
            self.probes[profile.path] = self._probe(profile)

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        return thread

    @staticmethod
    def _probe(profile: AgentProfile) -> ProbeResult:
        try:
            return probe_endpoint(
                profile.provider, profile.model, base_url=profile.base_url, timeout=6.0
            )
        except Exception as exc:
            return ProbeResult(profile.provider, profile.model, "", "error", str(exc))

    def refresh_sessions_async(self) -> None:
        def work() -> None:
            self.sessions = running_sessions()

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ CLI

    def root(self) -> Any:
        if self._root is None:
            from typer.main import get_command

            from nika.cli.main import app

            self._root = get_command(app)
        return self._root

    def cli_command(self, path: list[str]) -> Any:
        """Resolve a click command for ``nika <path>``; None when unknown."""
        key = tuple(path)
        with self._cli_lock:
            if key in self._cli_cache:
                return self._cli_cache[key]
            command = self.root()
            for name in path:
                if not hasattr(command, "get_command"):
                    command = None
                    break
                try:
                    command = command.get_command(_context(command), name)
                except Exception:
                    command = None
                if command is None:
                    break
            self._cli_cache[key] = command
            return command

    def subcommands(self, group: Any) -> list[str]:
        return list(group.list_commands(_context(group)))

    def run_cli(self, argv: list[str]) -> int:
        """Run ``nika <argv>`` in a subprocess attached to this terminal."""
        self.console.print(
            Text.assemble(
                ("$ ", "grey50"),
                ("nika ", f"bold {_ACCENT}"),
                (shlex.join(argv), "bold"),
            )
        )
        start = time.monotonic()
        proc = subprocess.Popen(
            [sys.executable, "-m", "nika.cli.main", *argv], env=os.environ.copy()
        )
        while True:
            try:
                code = proc.wait()
                break
            except KeyboardInterrupt:
                # The child got SIGINT too; let it shut down cleanly.
                continue
        elapsed = time.monotonic() - start
        style = "green" if code == 0 else "red"
        mark = "✓" if code == 0 else f"✗ exit {code}"
        self.console.print(Text(f"{mark} · {elapsed:.1f}s", style=style))
        self.refresh_sessions_async()
        return code

    def cli_handler(self, name: str) -> Callable[[list[str]], None]:
        def handler(args: list[str]) -> None:
            if args:
                self.run_cli([name, *args])
                return
            argv = self.form_for_path([name])
            if argv:
                self.run_cli(argv)

        return handler

    def form_for_path(
        self, path: list[str], values: dict[str, Any] | None = None, title: str = ""
    ) -> list[str] | None:
        """Pick a subcommand if needed, then edit its options in a form."""
        command = self.cli_command(path)
        if command is None:
            self.console.print(f"[red]Unknown command: nika {' '.join(path)}[/]")
            return None
        while hasattr(command, "list_commands"):
            subs = self.subcommands(command)
            choices = [
                (s, f"{s:<12} {_short_help(self.cli_command([*path, s]))}")
                for s in subs
            ]
            sub = choose(f"nika {' '.join(path)} — choose a subcommand", choices)
            if sub is None:
                return None
            path = [*path, sub]
            command = self.cli_command(path)
        form = Form(path, command, dict(values or {}))
        if not form.params():
            return form.argv()
        return self.edit_form(form, title)

    # ------------------------------------------------------------------ forms

    def edit_form(self, form: Form, title: str = "") -> list[str] | None:
        last: Any = "__run__"
        help_line = (
            (form.command.help or "").strip().splitlines()[0]
            if form.command.help
            else ""
        )
        while True:
            missing = form.missing()
            preview = "nika " + shlex.join(form.argv())
            run_label = (
                f"▶ Run   {preview}"
                if not missing
                else f"▶ Run   (first set: {', '.join(_param_label(p) for p in missing)})"
            )
            choices: list[tuple[Any, str]] = [("__run__", run_label)]
            for param in form.params():
                value = _format_value(form.values.get(param.name))
                req = "*" if param.required else " "
                choices.append(
                    (
                        param.name,
                        f"  {req}{_param_label(param):<30} {value:<28} {(getattr(param, 'help', '') or '')[:70]}",
                    )
                )
            choices.append(("__cancel__", "✕ Cancel"))
            heading = title or f"nika {' '.join(form.path)}"
            picked = choose(
                f"{heading}\n{help_line}\nPick a field to change, then ▶ Run. (* = required)",
                choices,
                default=last,
            )
            if picked in (None, "__cancel__"):
                return None
            if picked == "__run__":
                if not missing:
                    return form.argv()
                picked = missing[0].name
            last = picked
            param = next(p for p in form.params() if p.name == picked)
            value = self.edit_param(form, param)
            if value is _UNSET:
                form.values.pop(param.name, None)
            elif value is not None:
                form.values[param.name] = value

    def edit_param(self, form: Form, param: Any) -> Any:
        """Return a new value, _UNSET to clear it, or None to keep it."""
        current = form.values.get(param.name)
        label = _param_label(param)
        if getattr(param, "is_flag", False):
            if not param.secondary_opts:
                return _UNSET if current else True
            return self.pick(
                f"{label}",
                [(True, f"{label}"), (False, _long_opt(param.secondary_opts))],
                current,
                custom=False,
            )
        provider = self._provider(form, param)
        if provider is not None:
            return provider(form, param, current)
        choices = getattr(param.type, "choices", None)
        if choices:
            return self.pick(
                label, [(c, str(c)) for c in choices], current, custom=False
            )
        return self.ask(param, current)

    def ask(self, param: Any, current: Any) -> Any:
        label = _param_label(param)
        many = getattr(param, "multiple", False) or param.nargs == -1
        hint = " (space-separated; quote values with spaces)" if many else ""
        if _empty(current):
            default = ""
        else:
            default = shlex.join(current) if many else str(current)
        self.console.print(
            f"[grey50]{escape(getattr(param, 'help', '') or '')}{hint}[/]"
        )
        raw = text_prompt(f"{label}: ", default=default).strip()
        if not raw:
            return _UNSET
        return shlex.split(raw) if many else raw

    def pick(
        self,
        title: str,
        choices: list[tuple[Any, str]],
        current: Any = None,
        *,
        custom: bool = True,
        unset: bool = True,
    ) -> Any:
        """Choose a value; adds 'type a value' and 'use default' entries."""
        extra: list[tuple[Any, str]] = []
        if custom:
            extra.append(("__custom__", "✎ Type a value…"))
        if unset:
            extra.append(("__unset__", "∅ Use the default"))
        picked = choose(title, [*choices, *extra], default=current)
        if picked is None:
            return None
        if picked == "__unset__":
            return _UNSET
        if picked == "__custom__":
            raw = text_prompt(f"{title}: ").strip()
            return raw or None
        return picked

    def _provider(
        self, form: Form, param: Any
    ) -> Callable[[Form, Any, Any], Any] | None:
        name = param.name
        path = form.path
        if name == "run_config":
            return lambda f, p, c: self._pick_profile_value(c)
        if name == "session_id":
            if getattr(param, "multiple", False):
                return None
            return lambda f, p, c: self.pick_session(c) or None
        if name == "problem" and path == ["agent", "run"]:
            return lambda f, p, c: t.label if (t := self.pick_task(c)) else None
        if name in ("problem", "problems", "failure") and path != ["agent", "run"]:
            return self._pick_failure
        if name in ("scenario", "env") or (name == "name" and path == ["env", "run"]):
            return self._pick_scenario
        if name == "size":
            return lambda f, p, c: self.pick("Topology size", _SIZES, c, custom=False)
        if name == "host" and path == ["exec"]:
            return lambda f, p, c: self._pick_host(f.values.get("session_id"), c)
        if name == "agent_type":
            from nika.cli.commands.agent import SUPPORTED_AGENT_TYPES

            return lambda f, p, c: self.pick(
                "Agent type", [(a, a) for a in SUPPORTED_AGENT_TYPES], c
            )
        if name == "llm_provider":
            from nika.cli.commands.agent import SUPPORTED_LLM_PROVIDERS

            return lambda f, p, c: self.pick(
                "LLM provider", [(a, a) for a in SUPPORTED_LLM_PROVIDERS], c
            )
        if name == "reasoning_effort":
            from agent.cli.codex.codex_worker import REASONING_EFFORT_LEVELS

            return lambda f, p, c: self.pick(
                "Reasoning effort", [(a, a) for a in REASONING_EFFORT_LEVELS], c
            )
        if name == "release":
            return lambda f, p, c: self.pick_release(c)
        if name == "split":
            return lambda f, p, c: self.pick(
                "Split", [("dev", "dev"), ("test", "test")], c
            )
        if name == "task_id" and path == ["case", "run"]:
            return lambda f, p, c: self._pick_task_id()
        return None

    # ---------------------------------------------------------------- pickers

    def pick_task(self, current: str | None = None, query: str = "") -> Task | None:
        tasks = self._load_tasks()
        if not tasks:
            return None
        if query:
            exact = [t for t in tasks if t.label == query]
            if exact:
                return exact[0]
            words = query.casefold().split()
            tasks = [
                t
                for t in tasks
                if all(
                    w in f"{t.label} {t.domain} {t.description}".casefold()
                    for w in words
                )
            ] or tasks
        width = min(max(len(t.label) for t in tasks), 48)
        choices = [
            (t, f"{t.label:<{width}}  {t.domain[:31]:<31}  {t.description[:80]}")
            for t in tasks
        ]
        default = next((t for t in tasks if t.label == current), None)
        return choose(
            f"Choose a task  ({len(tasks)} tasks · type to search label, domain, or symptom)",
            choices,
            default=default,
        )

    def _load_tasks(self) -> list[Task]:
        if not self.tasks.ready:
            with self.console.status("Loading the task catalog…", spinner="dots"):
                self.tasks.wait()
        if self.tasks.error and not self._warned_tasks:
            self._warned_tasks = True
            self.console.print(f"[yellow]{escape(self.tasks.error)}[/]")
        return self.tasks.wait()

    def _pick_failure(self, form: Form, param: Any, current: Any) -> Any:
        from nika.problems.registry import get_problem_class, list_avail_problem_names

        choices = []
        for name in sorted(list_avail_problem_names()):
            cls = get_problem_class(name)
            desc = " ".join(str(getattr(cls, "description", "") or "").split())
            choices.append((name, f"{name:<40} {desc[:80]}"))
        value = self.pick(
            "Choose a failure",
            choices,
            current[0] if isinstance(current, list) and current else current,
        )
        if value is None or value is _UNSET:
            return value
        many = getattr(param, "multiple", False) or param.nargs == -1
        return [value] if many else value

    def _pick_scenario(self, form: Form, param: Any, current: Any) -> Any:
        from nika.net_env.net_env_pool import (
            list_all_net_envs,
            scenario_requires_topo_size,
        )

        envs = list_all_net_envs()
        choices = [
            (
                name,
                f"{name:<28} {', '.join(envs[name].SUPPORTED_BACKENDS):<22} {'sizes s/m/l' if scenario_requires_topo_size(name) else ''}",
            )
            for name in sorted(envs)
        ]
        value = self.pick(
            "Choose a network scenario",
            choices,
            current[0] if isinstance(current, list) and current else current,
        )
        if value is None or value is _UNSET:
            return value
        sized = any(p.name == "size" for p in form.command.params)
        if sized and scenario_requires_topo_size(value) and not form.values.get("size"):
            size = self.pick(f"{value} topology size", _SIZES, custom=False)
            if isinstance(size, str):
                form.values["size"] = size
        return [value] if getattr(param, "multiple", False) else value

    def _pick_host(self, session_id: str | None, current: Any) -> Any:
        sid = session_id or self.pick_session()
        if not sid:
            return None
        try:
            with self.console.status("Listing lab hosts…", spinner="dots"):
                hosts = session_hosts(sid)
        except Exception as exc:
            self.console.print(f"[red]{escape(str(exc))}[/]")
            return None
        return self.pick(
            f"Host in {sid}", [(h, h) for h in hosts], current, unset=False
        )

    def _pick_task_id(self) -> Any:
        from nika.workflows.benchmark.trials import task_id_for_row

        task = self.pick_task()
        if task is None:
            return None
        rows = task.presets
        choices = [
            (
                task_id_for_row(r),
                ", ".join(f"{k}={v}" for k, v in r["inject"].items()) or "defaults",
            )
            for r in rows
        ]
        return self.pick(
            f"{task.label} — choose an injection preset",
            choices,
            custom=False,
            unset=False,
        )

    def pick_session(self, current: str | None = None) -> str | None:
        with self.console.status("Listing sessions…", spinner="dots"):
            sessions = running_sessions()
        self.sessions = sessions
        if not sessions:
            self.console.print(
                "[yellow]No running sessions.[/] Start one with [bold]/run[/], [bold]/lab[/], or [bold]/tasks[/]."
            )
            return None
        if len(sessions) == 1 and current is None:
            return sessions[0]["session_id"]
        choices = [(s["session_id"], _session_line(s)) for s in sessions]
        return choose("Choose a running session", choices, default=current)

    def pick_release(self, current: Any = None) -> Any:
        items = releases()
        choices = [(v, f"{v}{'  (deprecated)' if dep else ''}") for v, dep in items]
        return self.pick("Choose a benchmark release", choices, current)

    def _pick_profile_value(self, current: Any) -> Any:
        profile = self.pick_profile(current)
        return profile.display_path if profile else None

    def pick_profile(
        self, current: Any = None, title: str = "Choose an agent profile"
    ) -> AgentProfile | None:
        profiles = list_profiles()
        if not profiles:
            self.console.print("[yellow]No run configs under config/.[/]")
            return None
        self._probe_all(profiles, show=False)
        choices = []
        for p in profiles:
            badge, _ = probe_badge(self.probes.get(p.path))
            kind = "template" if p.template else "config"
            choices.append(
                (
                    p,
                    f"{p.name:<30} {kind:<8} {p.agent_type:<16} {p.provider + '/' + p.model:<38} {badge}",
                )
            )
        default = next(
            (
                p
                for p in profiles
                if current
                and (str(p.path) == str(current) or p.display_path == str(current))
            ),
            next(
                (p for p in profiles if self.profile and p.path == self.profile.path),
                None,
            ),
        )
        return choose(f"{title}  (endpoint status is live)", choices, default=default)

    def _probe_all(self, profiles: list[AgentProfile], *, show: bool) -> None:
        """Probe every profile endpoint in parallel, optionally as a live table."""
        todo = [p for p in profiles if self.probes.get(p.path) is None]
        if not todo:
            if show:
                self.console.print(self._profiles_table(profiles))
            return
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(self._probe, p): p for p in todo}
            if show:
                with Live(
                    self._profiles_table(profiles),
                    console=self.console,
                    refresh_per_second=8,
                ) as live:
                    for future in as_completed(futures):
                        self.probes[futures[future].path] = future.result()
                        live.update(self._profiles_table(profiles))
            else:
                with self.console.status(
                    f"Checking {len(todo)} endpoints…", spinner="dots"
                ):
                    for future in as_completed(futures):
                        self.probes[futures[future].path] = future.result()

    def _profiles_table(self, profiles: list[AgentProfile]) -> Table:
        table = Table(
            box=None, header_style="bold grey70", pad_edge=False, expand=False
        )
        for column in ("", "PROFILE", "AGENT", "PROVIDER / MODEL", "ENDPOINT"):
            table.add_column(column)
        for p in profiles:
            badge, style = probe_badge(self.probes.get(p.path))
            active = self.profile is not None and p.path == self.profile.path
            name = Text(p.display_path, style="bold" if active else "")
            if p.template:
                name.append("  template", style="grey50")
            table.add_row(
                Text("▶" if active else " ", style=_ACCENT),
                name,
                p.agent_type,
                f"{p.provider}/{p.model or '—'}",
                Text(badge, style=style),
            )
        return table

    # ---------------------------------------------------------- slash commands

    def cmd_run(self, args: list[str]) -> None:
        if args and args[0].startswith("-"):
            self.run_cli(["agent", "run", *args])
            return
        task = self.pick_task(query=" ".join(args))
        if task is None:
            return
        self._run_task(task)

    def _run_task(self, task: Task) -> None:
        sets = self._pick_injection(task)
        if sets is None:
            return
        profile = self.pick_profile(
            title=f"Which agent should troubleshoot {task.label}?"
        )
        if profile is None or not self._endpoint_gate(profile):
            return
        form = Form(
            ["agent", "run"],
            self.cli_command(["agent", "run"]),
            {"problem": task.label, "run_config": profile.display_path, "sets": sets},
        )
        argv = self.edit_form(
            form, f"Run {profile.agent_type} ({profile.model}) on {task.label}"
        )
        if argv:
            self.run_cli(argv)

    def _pick_injection(self, task: Task) -> list[str] | None:
        presets = task.presets
        choices: list[tuple[Any, str]] = [
            ("default", "Default injection (NIKA picks the targets)")
        ]
        for i, row in enumerate(presets[:200]):
            params = ", ".join(f"{k}={v}" for k, v in row["inject"].items())
            choices.append((i, f"Preset  {params}"))
        if len(choices) == 1:
            return []
        picked = choose(
            f"{task.label}\n{task.description}\nChoose where to inject the fault",
            choices,
        )
        if picked is None:
            return None
        if picked == "default":
            return []
        return [f"{k}={v}" for k, v in presets[picked]["inject"].items()]

    def _endpoint_gate(self, profile: AgentProfile) -> bool:
        """Probe *profile* and ask before running against a broken endpoint."""
        result = self.probes.get(profile.path)
        if result is None:
            with self.console.status(
                f"Checking {profile.provider} endpoint…", spinner="dots"
            ):
                result = self._probe(profile)
            self.probes[profile.path] = result
        self.console.print(self._probe_panel(profile, result))
        if result.status == OK:
            return True
        picked = choose(
            "The endpoint check did not pass. Continue anyway?",
            [("no", "No, go back"), ("yes", "Yes, run anyway")],
        )
        return picked == "yes"

    def _probe_panel(self, profile: AgentProfile, result: ProbeResult) -> Panel:
        badge, style = probe_badge(result)
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="grey62")
        grid.add_column()
        grid.add_row("profile", profile.display_path)
        grid.add_row("agent", profile.agent_type)
        grid.add_row("model", f"{profile.provider} / {profile.model or '—'}")
        grid.add_row("endpoint", result.endpoint or "—")
        grid.add_row("status", Text(f"{badge}  {result.detail}", style=style))
        if result.status in (MODEL_NOT_LISTED,) and result.models:
            grid.add_row(
                "served",
                ", ".join(result.models[:8]) + (" …" if len(result.models) > 8 else ""),
            )
        if result.status == NO_CREDENTIALS:
            grid.add_row("fix", _credential_hint(profile.provider))
        if result.status == AUTH_FAILED:
            grid.add_row("fix", f"Check {_credential_hint(profile.provider)}")
        return Panel(
            grid,
            title="endpoint check",
            title_align="left",
            border_style=style,
            expand=False,
        )

    def cmd_tasks(self, args: list[str]) -> None:
        task = self.pick_task(query=" ".join(args))
        if task is None:
            return
        from nika.workflows.benchmark.trials import task_id_for_row

        self.console.print(
            Panel(
                Text.assemble(
                    (task.label, "bold"),
                    "\n",
                    (
                        f"scenario {task.scenario}  ·  failure {task.problem}  ·  {task.domain}\n",
                        "grey62",
                    ),
                    task.description,
                    (f"\n{len(task.presets)} injection preset(s)", "grey50"),
                ),
                border_style=_ACCENT,
                expand=False,
            )
        )
        action = choose(
            "What next?",
            [
                ("run", "▶ Run an agent on this task"),
                (
                    "lab",
                    "⚑ Start the lab with this fault, no agent (troubleshoot it yourself)",
                ),
                ("describe", "ⓘ Describe the failure"),
                ("cli", "⌘ Show the CLI command"),
            ],
        )
        if action == "run":
            self._run_task(task)
        elif action == "lab" and task.presets:
            task_id = self.pick(
                "Choose an injection preset",
                [
                    (
                        task_id_for_row(r),
                        ", ".join(f"{k}={v}" for k, v in r["inject"].items()),
                    )
                    for r in task.presets
                ],
                custom=False,
                unset=False,
            )
            if isinstance(task_id, str):
                self.run_cli(["case", "run", task_id])
        elif action == "describe":
            self.run_cli(["failure", "describe", task.problem])
        elif action == "cli":
            run_config = (
                self.profile.display_path if self.profile else "config/nika.yaml"
            )
            self.console.print(
                f"nika agent run --run-config {run_config} --problem {task.label}",
                style="bold",
                soft_wrap=True,
            )

    def cmd_agents(self, args: list[str]) -> None:
        profiles = list_profiles()
        if not profiles:
            self.console.print(
                "[yellow]No run configs under config/. Copy config/nika.example.yaml to config/nika.yaml.[/]"
            )
            return
        for p in profiles:
            self.probes[p.path] = None  # always re-check here
        self._probe_all(profiles, show=True)
        self.console.print(
            "[grey50]Templates (*.example.yaml) work as-is; copy one to config/ to customize it.[/]"
        )
        profile = self.pick_profile(title="Pick a profile to make it active")
        if profile is not None:
            self._activate(profile)

    def cmd_use(self, args: list[str]) -> None:
        if not args:
            profile = self.pick_profile(title="Switch the active agent profile")
            if profile is not None:
                self._activate(profile)
            return
        path = resolve_run_config_path(args[0])
        if not path.is_file():
            self.console.print(f"[red]No such file: {escape(str(path))}[/]")
            return
        self._activate(load_profile(path))

    def _activate(self, profile: AgentProfile) -> None:
        self.profile = profile
        os.environ[ENV_RUN_CONFIG] = str(profile.path)
        if self.probes.get(profile.path) is None:
            self.probe_async(profile)
        self.console.print(
            f"Active profile: [bold]{escape(profile.display_path)}[/]  "
            f"{escape(profile.agent_type)} · {escape(profile.provider)}/{escape(profile.model or '—')}"
        )

    def cmd_check(self, args: list[str]) -> None:
        profile = (
            load_profile(resolve_run_config_path(args[0])) if args else self.profile
        )
        if profile is None:
            self.console.print("[yellow]No active profile. Use /agents.[/]")
            return
        with self.console.status(
            f"Checking {profile.provider} endpoint…", spinner="dots"
        ):
            result = self._probe(profile)
        self.probes[profile.path] = result
        self.console.print(self._probe_panel(profile, result))
        self.console.print(
            f"[grey50]CLI: nika agent check --run-config {escape(profile.display_path)}[/]"
        )

    def cmd_sessions(self, args: list[str]) -> None:
        sid = self.pick_session(current="")
        if not sid:
            return
        actions = [
            ("shell", "⌨ Open a shell on a lab host"),
            ("agent", "▶ Run an agent on this session"),
            ("inject", "⚡ Inject another failure"),
            ("failures", "⚑ Show injected failures"),
            ("inspect", "ⓘ Inspect the session"),
            ("containers", "▦ List lab containers"),
            ("metrics", "∑ Evaluate (metrics)"),
            ("close", "✕ Close the session and its lab"),
        ]
        action = choose(f"Session {sid}", actions)
        if action == "shell":
            self.cmd_shell([], session_id=sid)
        elif action == "agent":
            profile = self.pick_profile(
                title="Which agent should troubleshoot this session?"
            )
            if profile and self._endpoint_gate(profile):
                values = {"session_id": sid, "run_config": profile.display_path}
                argv = self.edit_form(
                    Form(["agent", "run"], self.cli_command(["agent", "run"]), values)
                )
                if argv:
                    self.run_cli(argv)
        elif action == "inject":
            argv = self.form_for_path(["failure", "inject"], {"session_id": sid})
            if argv:
                self.run_cli(argv)
        elif action == "failures":
            self.run_cli(["failure", "ps", "--session_id", sid])
        elif action == "inspect":
            self.run_cli(["session", "inspect", "--session_id", sid])
        elif action == "containers":
            self.run_cli(["session", "containers", "--session_id", sid])
        elif action == "metrics":
            self.run_cli(["eval", "metrics", "--session_id", sid])
        elif action == "close":
            self.run_cli(["session", "close", "--session_id", sid])

    def cmd_lab(self, args: list[str]) -> None:
        if args:
            self.run_cli(["env", "run", *args])
            return
        argv = self.form_for_path(["env", "run"], title="Deploy a network scenario")
        if argv:
            self.run_cli(argv)

    def cmd_inject(self, args: list[str]) -> None:
        if args:
            self.run_cli(["failure", "inject", *args])
            return
        sid = self.pick_session()
        if not sid:
            return
        argv = self.form_for_path(["failure", "inject"], {"session_id": sid})
        if argv:
            self.run_cli(argv)

    def cmd_shell(self, args: list[str], session_id: str | None = None) -> None:
        if args:
            self.run_cli(["exec", *args])
            return
        sid = session_id or self.pick_session()
        if not sid:
            return
        host = self._pick_host(sid, None)
        if not isinstance(host, str):
            return
        from nika.workflows.exec.command import exec_command_in_host

        self.console.print(
            f"[grey50]Commands run via [bold]nika exec --session_id {escape(sid)} {escape(host)} <cmd>[/bold]. "
            "Type [bold]exit[/bold] or Ctrl+D to return.[/]"
        )
        shell = PromptSession(history=InMemoryHistory(), style=_PT_STYLE)
        while True:
            try:
                line = shell.prompt(
                    [
                        ("class:host", host),
                        ("class:dim", f" @ {sid} "),
                        ("class:arrow", "$ "),
                    ]
                ).strip()
            except KeyboardInterrupt:
                continue
            except EOFError:
                break
            if line in ("exit", "quit", "logout"):
                break
            if not line:
                continue
            try:
                output = exec_command_in_host(
                    host=host, command=line, session_id=sid, timeout=30.0
                )
            except Exception as exc:
                self.console.print(f"[red]{escape(str(exc))}[/]")
                continue
            self.console.print(Text(output.rstrip("\n")))

    def cmd_bench(self, args: list[str]) -> None:
        if args:
            self.run_cli(["benchmark", *args])
            return
        action = choose(
            "Benchmark",
            [
                ("run", "▶ Run a frozen release with an agent"),
                ("list", "≣ List the tasks in a release"),
                ("releases", "≣ List releases"),
                ("other", "… Other benchmark commands"),
            ],
        )
        if action == "releases":
            self.run_cli(["benchmark", "list"])
        elif action in ("run", "list"):
            release = self.pick_release()
            if not isinstance(release, str):
                return
            split = self.pick(
                "Split",
                [("dev", "dev   development split"), ("test", "test  held-out split")],
                custom=False,
                unset=False,
            )
            if not isinstance(split, str):
                return
            if action == "list":
                self.run_cli(
                    ["benchmark", "list", "--release", release, "--split", split]
                )
                return
            profile = self.pick_profile(
                title=f"Which agent should run {release} ({split})?"
            )
            if profile is None or not self._endpoint_gate(profile):
                return
            values = {
                "release": release,
                "split": split,
                "run_config": profile.display_path,
            }
            argv = self.edit_form(
                Form(
                    ["benchmark", "run"], self.cli_command(["benchmark", "run"]), values
                ),
                f"Run nika-bench {release} ({split}) with {profile.agent_type}",
            )
            if argv:
                self.run_cli(argv)
        elif action == "other":
            argv = self.form_for_path(["benchmark"])
            if argv:
                self.run_cli(argv)

    def cmd_help(self, args: list[str]) -> None:
        if args:
            name = args[0].lstrip("/")
            target = _CLI_ALIASES.get(name, [name])
            self.run_cli([*target, *args[1:], "--help"])
            return
        for group in ("Guided", "CLI", "TUI"):
            table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2))
            table.add_column(style=f"bold {_ACCENT}", no_wrap=True)
            table.add_column(style="grey62", no_wrap=True)
            table.add_column()
            for cmd in self.commands.values():
                if cmd.group == group:
                    table.add_row(f"/{cmd.name}", cmd.usage, cmd.help)
            self.console.print(Text(group.upper(), style="bold grey50"))
            self.console.print(table)
            self.console.print()
        self.console.print(
            "[grey50]CLI commands take regular arguments (`/session ps -a`), or open a form when "
            "called bare. Tab completes commands, subcommands, options, and task labels. "
            "Lines starting with `nika ` run as-is.[/]"
        )

    def cmd_quit(self, args: list[str]) -> None:
        raise EOFError

    # ------------------------------------------------------------------- home

    def home(self, *, animate: bool) -> None:
        diagram = art.diagram_of_the_day()
        if self.sessions is None:
            self.refresh_sessions_async()
        self.tasks.start()

        def frame(lit: int | None) -> Group:
            return self._home_frame(diagram, lit)

        if animate and self.console.is_terminal:
            with Live(frame(0), console=self.console, refresh_per_second=20) as live:
                for lit in range(1, len(diagram.path) + 2):
                    time.sleep(0.28)
                    live.update(frame(lit))
                # Give the endpoint probe a moment so the panel shows a result.
                deadline = time.monotonic() + 2.5
                while (
                    time.monotonic() < deadline
                    and self.profile
                    and self.probes.get(self.profile.path) is None
                ):
                    time.sleep(0.1)
                live.update(frame(None))
        else:
            self.console.print(frame(None))

    def _home_frame(self, diagram: art.Diagram, lit: int | None) -> Group:
        picture = art.render_diagram(diagram, lit)
        if lit is None:
            picture.append(f"\n\n✗ {diagram.incident}", style="red")
        diagram_panel = Panel(
            picture,
            title=f"[bold]diagram of the day[/] · {diagram.scenario}",
            subtitle=f"[grey50]{diagram.title}[/]",
            border_style="#af5fff",
            padding=(1, 2),
        )
        status = Table.grid(padding=(0, 2))
        status.add_column(style="grey62", no_wrap=True)
        status.add_column()
        if self.profile:
            badge, style = probe_badge(self.probes.get(self.profile.path))
            status.add_row("profile", self.profile.display_path)
            status.add_row("agent", self.profile.agent_type)
            status.add_row(
                "model", f"{self.profile.provider} / {self.profile.model or '—'}"
            )
            status.add_row("endpoint", Text(badge, style=style))
        else:
            status.add_row("profile", Text("none — run /agents", style="yellow"))
        sessions = "…" if self.sessions is None else str(len(self.sessions))
        status.add_row("sessions", f"{sessions} running")
        status.add_row(
            "tasks",
            f"{len(self.tasks.wait(0))} labels" if self.tasks.ready else "loading…",
        )
        status.add_row("", "")
        for name, text in (
            ("/run", "run an agent on a task"),
            ("/tasks", "browse benchmark tasks"),
            ("/agents", "profiles + endpoint health"),
            ("/sessions", "running labs"),
            ("/help", "everything else"),
        ):
            status.add_row(
                Text(name, style=f"bold {_ACCENT}"), Text(text, style="grey70")
            )
        status_panel = Panel(
            status, title="[bold]workspace[/]", border_style=_ACCENT, padding=(1, 2)
        )
        body = Table.grid(padding=(0, 1))
        if self.console.width >= 96:
            body.add_column()
            body.add_column()
            body.add_row(diagram_panel, status_panel)
        else:
            body.add_column()
            body.add_row(diagram_panel)
            body.add_row(status_panel)
        return Group(Text(), art.logo(), Text(), body, Text())

    # ------------------------------------------------------------------- loop

    def toolbar(self) -> list[tuple[str, str]]:
        parts: list[tuple[str, str]] = [("class:tb.key", " ◆ ")]
        if self.profile:
            result = self.probes.get(self.profile.path)
            badge, style = probe_badge(result)
            cls = {"green": "tb.ok", "yellow": "tb.warn", "red": "tb.bad"}.get(
                style, "tb.key"
            )
            parts += [
                ("class:tb.val", f"{self.profile.name} "),
                ("class:tb.key", "│ "),
                (
                    "class:tb.val",
                    f"{self.profile.agent_type} · {self.profile.provider}/{self.profile.model or '—'} ",
                ),
                (f"class:{cls}", f"{badge} "),
            ]
        else:
            parts.append(("class:tb.warn", "no profile — /agents "))
        sessions = "…" if self.sessions is None else str(len(self.sessions))
        parts += [("class:tb.key", "│ sessions "), ("class:tb.val", f"{sessions} ")]
        tasks = str(len(self.tasks.wait(0))) if self.tasks.ready else "…"
        parts += [("class:tb.key", "│ tasks "), ("class:tb.val", f"{tasks} ")]
        if time.monotonic() < self._exit_armed_until:
            parts += [("class:tb.alert", "│ Press Ctrl+C again to exit")]
        else:
            parts += [
                ("class:tb.key", "│ / commands · Tab complete · Ctrl+C twice to quit")
            ]
        return parts

    def run(self) -> None:
        from nika.utils.logger import set_console_events

        # Lifecycle events print from CLI subprocesses, not from background loaders.
        set_console_events(False)
        self.tasks.start()
        self.refresh_sessions_async()
        self.home(animate=True)
        session = PromptSession(
            history=_history(),
            completer=ThreadedCompleter(SlashCompleter(self)),
            complete_while_typing=True,
            bottom_toolbar=self.toolbar,
            refresh_interval=0.5,
            style=_PT_STYLE,
        )
        while True:
            try:
                line = session.prompt(
                    [("class:prompt", "nika"), ("class:arrow", " ❯ ")]
                )
            except KeyboardInterrupt:
                # First Ctrl+C clears the line; a second one within the window quits.
                if time.monotonic() < self._exit_armed_until:
                    break
                self._exit_armed_until = time.monotonic() + _EXIT_WINDOW_SEC
                continue
            except EOFError:
                break
            self._exit_armed_until = 0.0
            try:
                self.dispatch(line)
            except KeyboardInterrupt:
                self.console.print("[grey50]Cancelled.[/]")
            except EOFError:
                break
            except Exception as exc:  # keep the shell alive
                self.console.print(f"[red]{type(exc).__name__}: {escape(str(exc))}[/]")
        self.console.print(
            "[grey50]Bye. Every action above is also a plain `nika` command.[/]"
        )

    def dispatch(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            words = shlex.split(line)
        except ValueError as exc:
            self.console.print(f"[red]{escape(str(exc))}[/]")
            return
        if words[0] == "nika":
            self.run_cli(words[1:])
            return
        if not line.startswith("/"):
            self.console.print(
                "Commands start with [bold]/[/]. Try [bold]/help[/] or [bold]/run[/]."
            )
            return
        name, args = words[0][1:], words[1:]
        command = self.commands.get(name)
        if command is None:
            prefixed = [c for c in self.commands if c.startswith(name)]
            if len(prefixed) == 1:
                command = self.commands[prefixed[0]]
        if command is None:
            close = [c for c in self.commands if c.startswith(name)]
            close = close or difflib.get_close_matches(name, self.commands, n=3)
            hint = (
                f" Did you mean {', '.join('/' + c for c in close)}?" if close else ""
            )
            self.console.print(f"[red]Unknown command /{escape(name)}.[/]{hint}")
            return
        command.handler(args)


_CLI_ALIASES = {
    "run": ["agent", "run"],
    "tasks": ["case", "browse"],
    "agents": ["agent", "list"],
    "check": ["agent", "check"],
    "sessions": ["session", "ps"],
    "lab": ["env", "run"],
    "inject": ["failure", "inject"],
    "shell": ["exec"],
    "bench": ["benchmark", "run"],
}


def _lazy_cli_names() -> list[str]:
    return [name for name in _LAZY_HELP if name != "tui"]


def _lazy_help() -> dict[str, str]:
    from nika.cli.main import LAZY_COMMANDS

    return {name: spec.help for name, spec in LAZY_COMMANDS.items()}


_LAZY_HELP = _lazy_help()


def _session_line(session: dict[str, Any]) -> str:
    problems = session.get("problem_names") or []
    failure = "+".join(map(str, problems)) if problems else "no failure"
    agent = session.get("agent_type") or "no agent"
    return f"{session.get('session_id', '—'):<28} {env_id_from_lab(session.get('lab_name')):<24} {failure:<30} {agent}"


def _credential_hint(provider: str) -> str:
    return {
        "openai": "OPENAI_API_KEY in .env",
        "anthropic": "ANTHROPIC_API_KEY in .env",
        "deepseek": "DEEPSEEK_API_KEY in .env",
        "custom": "agent.base_url in the profile (and NIKA_CUSTOM_API_KEY in .env if needed)",
    }.get(provider, ".env credentials")


def _history() -> Any:
    try:
        path = Path.home() / ".cache" / "nika" / "tui_history"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        return FileHistory(str(path))
    except OSError:
        return InMemoryHistory()


def run_tui(run_config: str | None = None) -> None:
    NikaTui(run_config).run()
