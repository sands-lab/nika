"""Commands for running diagnosis agents."""

import typer

from agent.cli.codex.codex_worker import REASONING_EFFORT_LEVELS
from nika.run_config.loader import (
    ENV_RUN_CONFIG,
    export_run_config_env,
    load_run_config,
    merge_cli,
    persist_effective_run_config,
    set_run_config,
)
from nika.run_config.legacy import warn_legacy_operational_env
from nika.utils.agent_config import apply_custom_provider_env

SUPPORTED_AGENT_TYPES = (
    "byo.langgraph",
    "byo.mcp_agent",
    "byo.autogen",
    "cli.codex",
    "cli.claude",
    "community.sade",
    "sdk.claude_sdk",
    "sdk.codex_sdk",
)
SUPPORTED_LLM_PROVIDERS = ("openai", "anthropic", "deepseek", "custom")

agent_app = typer.Typer(help="Troubleshooting agents.")


def _parse_set_options(raw_items: list[str] | None) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for raw in raw_items or []:
        if "=" not in raw:
            raise typer.BadParameter(f"Invalid --set value {raw!r}. Use key=value.")
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise typer.BadParameter(
                f"Invalid --set value {raw!r}. Key cannot be empty."
            )
        overrides[key] = value.strip()
    return overrides


def _activate_run_config(
    *,
    run_config: str | None,
    agent_type: str | None,
    llm_provider: str | None,
    model: str | None,
    max_steps: int | None,
    reasoning_effort: str | None,
    access_role: str | None,
    base_url: str | None,
    sandbox_keep_container: bool,
    sandbox_cpus: str | None,
    sandbox_memory: str | None,
    sandbox_offline_sdk_wheels: bool,
    sandbox_upstream_proxy: str | None,
) -> None:
    warn_legacy_operational_env()
    cfg_path = export_run_config_env(run_config)
    cfg = load_run_config(cfg_path)
    cfg = merge_cli(
        cfg,
        agent_type=agent_type,
        llm_provider=llm_provider,
        model=model,
        max_steps=max_steps,
        reasoning_effort=reasoning_effort,
        access_role=access_role,
        base_url=base_url,
        sandbox_keep=sandbox_keep_container or None,
        sandbox_cpus=sandbox_cpus,
        sandbox_memory=sandbox_memory,
        sandbox_offline_sdk_wheels=sandbox_offline_sdk_wheels or None,
        sandbox_upstream_proxy=sandbox_upstream_proxy,
    )
    set_run_config(cfg)
    persist_effective_run_config(cfg)
    apply_custom_provider_env(cfg)


@agent_app.command("list")
def agent_list() -> None:
    """Print supported agent types and LLM providers."""
    typer.echo("agent_types:")
    for agent_type in SUPPORTED_AGENT_TYPES:
        typer.echo(f"  {agent_type}")
    typer.echo("llm_providers:")
    for provider in SUPPORTED_LLM_PROVIDERS:
        typer.echo(f"  {provider}")
    typer.echo(
        "reasoning_effort (byo.langgraph, byo.mcp_agent, byo.autogen, "
        "cli.codex, sdk.codex_sdk):"
    )
    for level in REASONING_EFFORT_LEVELS:
        typer.echo(f"  {level}")


@agent_app.command("run")
def agent_run(
    agent_type: str | None = typer.Option(
        None,
        "-a",
        "--agent",
        help="Agent implementation (default: agent.type in run config).",
    ),
    llm_provider: str | None = typer.Option(
        None,
        "-p",
        "--provider",
        help="LLM provider: openai, anthropic, deepseek, custom.",
    ),
    model: str | None = typer.Option(
        None,
        "-m",
        "--model",
        help="Model id (default: agent.model in run config).",
    ),
    max_steps: int | None = typer.Option(
        None,
        "-n",
        "--max-steps",
        help="Max steps per phase (default: agent.max_steps in run config).",
    ),
    reasoning_effort: str | None = typer.Option(
        None,
        "-e",
        "--reasoning-effort",
        help=(
            "Reasoning effort for byo agents (openai/custom; anthropic on "
            "langgraph), cli.codex, and sdk.codex_sdk: none, minimal, low, "
            "medium, high, xhigh. byo.mcp_agent supports none/low/medium/high."
        ),
    ),
    access_role: str | None = typer.Option(
        None, "--role", help="Diagnosis access role (default: agent.access.role)."
    ),
    base_url: str | None = typer.Option(
        None,
        "--base-url",
        help=(
            "Inference endpoint URL (default: agent.custom.base_url). "
            "Required for provider=custom; also overrides OpenAI/Anthropic base URL."
        ),
    ),
    run_config: str | None = typer.Option(
        None,
        "--run-config",
        envvar=ENV_RUN_CONFIG,
        help="Path to config/nika.yaml (default: config/nika.yaml).",
    ),
    problem: str | None = typer.Option(
        None,
        "--problem",
        help=(
            "Task label: {scenario}_{problem} or "
            "{scenario}_{s|m|l}_{problem}. Deploys the lab, injects the fault, "
            "runs the agent, then closes the session."
        ),
    ),
    sets: list[str] | None = typer.Option(
        None,
        "--set",
        help="Task mode: override inject parameters as key=value.",
    ),
    result_dir: str | None = typer.Option(
        None,
        "--result_dir",
        help="Task mode: results parent directory (default: nika.result_dir in run config).",
    ),
    session_id: str | None = typer.Option(
        None, "--session_id", help="Target session id (session mode only)."
    ),
    sandbox_keep_container: bool = typer.Option(
        False,
        "--sandbox-keep-container",
        help="Do not remove the sbx sandbox after the agent exits (debug).",
    ),
    sandbox_cpus: str | None = typer.Option(
        None,
        "--sandbox-cpus",
        help="CPU limit for the sandbox.",
    ),
    sandbox_memory: str | None = typer.Option(
        None,
        "--sandbox-memory",
        help="Memory limit for the sandbox (e.g. 8g).",
    ),
    sandbox_offline_sdk_wheels: bool = typer.Option(
        False,
        "--sandbox-offline-sdk-wheels",
        help=(
            "Stage host-cached SDK wheels into the sandbox (faster SDK/SADE "
            "deploys; avoids re-downloading deps on every sbx start)."
        ),
    ),
    sandbox_upstream_proxy: str | None = typer.Option(
        None,
        "--sandbox-proxy",
        help="Upstream proxy for sbx daemon.",
    ),
) -> None:
    """Run an end-to-end task, or run an agent on the current session."""
    if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORT_LEVELS:
        raise typer.BadParameter(
            f"reasoning_effort must be one of {', '.join(REASONING_EFFORT_LEVELS)}"
        )

    _activate_run_config(
        run_config=run_config,
        agent_type=agent_type,
        llm_provider=llm_provider,
        model=model,
        max_steps=max_steps,
        reasoning_effort=reasoning_effort,
        access_role=access_role,
        base_url=base_url,
        sandbox_keep_container=sandbox_keep_container,
        sandbox_cpus=sandbox_cpus,
        sandbox_memory=sandbox_memory,
        sandbox_offline_sdk_wheels=sandbox_offline_sdk_wheels,
        sandbox_upstream_proxy=sandbox_upstream_proxy,
    )

    if problem is not None:
        if session_id is not None:
            raise typer.BadParameter(
                "Use either --problem (task mode) or --session_id (session mode), not both."
            )
        _run_one_shot(
            problem_label=problem,
            agent_type=agent_type,
            llm_provider=llm_provider,
            model=model,
            max_steps=max_steps,
            sets=sets,
            result_dir=result_dir,
            reasoning_effort=reasoning_effort,
            sandbox_keep_container=sandbox_keep_container,
            sandbox_cpus=sandbox_cpus,
            sandbox_memory=sandbox_memory,
            sandbox_offline_sdk_wheels=sandbox_offline_sdk_wheels,
        )
        return

    if sets:
        raise typer.BadParameter("--set requires --problem.")
    if result_dir is not None:
        raise typer.BadParameter("--result_dir requires --problem (task mode).")

    from nika.workflows.agent.run import start_agent

    try:
        start_agent(
            agent_type,
            llm_provider,
            model,
            max_steps,
            session_id=session_id,
            reasoning_effort=reasoning_effort,
            sandbox_keep_container=sandbox_keep_container,
            sandbox_cpus=sandbox_cpus,
            sandbox_memory=sandbox_memory,
            sandbox_offline_sdk_wheels=sandbox_offline_sdk_wheels,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc


def _run_one_shot(
    *,
    problem_label: str,
    agent_type: str | None,
    llm_provider: str | None,
    model: str | None,
    max_steps: int | None,
    sets: list[str] | None,
    result_dir: str | None,
    reasoning_effort: str | None,
    sandbox_keep_container: bool,
    sandbox_cpus: str | None,
    sandbox_memory: str | None,
    sandbox_offline_sdk_wheels: bool,
) -> None:
    from nika.workflows.benchmark.run import run_single_case, validate_inject_params
    from nika.workflows.benchmark.task_label import (
        parse_task_label,
        resolve_default_inject_params,
    )

    try:
        scenario, topo_size, problem_name = parse_task_label(problem_label)
        overrides = _parse_set_options(sets)
        inject_params = resolve_default_inject_params(
            scenario,
            problem_name,
            topo_size,
            overrides=overrides or None,
        )
        validate_inject_params(problem_name, scenario, topo_size, inject_params)
    except (FileNotFoundError, ValueError, ImportError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    try:
        run_single_case(
            problem=problem_name,
            scenario=scenario,
            topo_size=topo_size,
            agent_type=agent_type,
            llm_provider=llm_provider,
            model=model,
            max_steps=max_steps,
            inject_params=inject_params,
            result_dir=result_dir,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
