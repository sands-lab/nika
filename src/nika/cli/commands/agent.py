"""Commands for running diagnosis agents."""

import os

import typer

from agent.cli.codex.codex_worker import REASONING_EFFORT_LEVELS
from agent.sandbox.config import (
    ENV_SANDBOX_CPUS,
    ENV_SANDBOX_ENV_FILE,
    ENV_SANDBOX_KEEP,
    ENV_SANDBOX_MEMORY,
    ENV_SANDBOX_OFFLINE_SDK_WHEELS,
    ENV_SANDBOX_UPSTREAM_PROXY,
)
from nika.utils.agent_config import (
    ENV_AGENT_TYPE,
    ENV_CODEX_REASONING_EFFORT,
    ENV_LLM_PROVIDER,
    ENV_MAX_STEPS,
    ENV_MODEL,
)

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
SUPPORTED_LLM_PROVIDERS = ("openai", "ollama", "deepseek", "custom")

agent_app = typer.Typer(help="Troubleshooting agents.")


@agent_app.command("list")
def agent_list() -> None:
    """Print supported agent types and LLM providers."""
    typer.echo("agent_types:")
    for agent_type in SUPPORTED_AGENT_TYPES:
        typer.echo(f"  {agent_type}")
    typer.echo("llm_providers (byo.langgraph only):")
    for provider in SUPPORTED_LLM_PROVIDERS:
        typer.echo(f"  {provider}")
    typer.echo("reasoning_effort (cli.codex, sdk.codex_sdk):")
    for level in REASONING_EFFORT_LEVELS:
        typer.echo(f"  {level}")


@agent_app.command("run")
def agent_run(
    agent_type: str | None = typer.Option(
        None,
        "-a",
        "--agent",
        envvar=ENV_AGENT_TYPE,
        help="Agent implementation (required unless NIKA_AGENT_TYPE is in .env).",
    ),
    llm_provider: str | None = typer.Option(
        None,
        "-p",
        "--provider",
        envvar=ENV_LLM_PROVIDER,
        help="LLM provider for byo.langgraph only: openai, ollama, deepseek, custom.",
    ),
    model: str | None = typer.Option(
        None,
        "-m",
        "--model",
        envvar=ENV_MODEL,
        help="Model id (required unless agent-specific NIKA_*_MODEL or NIKA_MODEL is in .env).",
    ),
    max_steps: int | None = typer.Option(
        None,
        "-n",
        "--max-steps",
        envvar=ENV_MAX_STEPS,
        help="Max steps per phase (required unless NIKA_MAX_STEPS is in .env; byo.langgraph, byo.mcp_agent, byo.autogen, community.sade, sdk.claude_sdk).",
    ),
    reasoning_effort: str | None = typer.Option(
        None,
        "-e",
        "--reasoning-effort",
        envvar=ENV_CODEX_REASONING_EFFORT,
        help="Codex model_reasoning_effort (cli.codex, sdk.codex_sdk): none, minimal, low, medium, high, xhigh.",
    ),
    session_id: str | None = typer.Option(
        None, "--session_id", help="Target session id."
    ),
    sandbox_env_file: str | None = typer.Option(
        None,
        "--sandbox-env-file",
        envvar=ENV_SANDBOX_ENV_FILE,
        help="Env file for credential resolution into the sandbox.",
    ),
    sandbox_keep_container: bool = typer.Option(
        False,
        "--sandbox-keep-container",
        envvar=ENV_SANDBOX_KEEP,
        help="Do not remove the sbx sandbox after the agent exits (debug).",
    ),
    sandbox_cpus: str | None = typer.Option(
        None,
        "--sandbox-cpus",
        envvar=ENV_SANDBOX_CPUS,
        help="CPU limit for the sandbox.",
    ),
    sandbox_memory: str | None = typer.Option(
        None,
        "--sandbox-memory",
        envvar=ENV_SANDBOX_MEMORY,
        help="Memory limit for the sandbox (e.g. 8g).",
    ),
    sandbox_offline_sdk_wheels: bool = typer.Option(
        False,
        "--sandbox-offline-sdk-wheels",
        envvar=ENV_SANDBOX_OFFLINE_SDK_WHEELS,
        help=(
            "Stage host-cached SDK wheels into the sandbox (faster SDK/SADE "
            "deploys; avoids re-downloading deps on every sbx start)."
        ),
    ),
    sandbox_upstream_proxy: str | None = typer.Option(
        None,
        "--sandbox-proxy",
        envvar=ENV_SANDBOX_UPSTREAM_PROXY,
        help="Upstream proxy for sbx daemon (e.g. http://127.0.0.1:7890 for Clash).",
    ),
) -> None:
    """Run the agent on the current session task."""
    from nika.workflows.agent.run import start_agent

    if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORT_LEVELS:
        raise typer.BadParameter(
            f"reasoning_effort must be one of {', '.join(REASONING_EFFORT_LEVELS)}"
        )

    if sandbox_upstream_proxy:
        os.environ[ENV_SANDBOX_UPSTREAM_PROXY] = sandbox_upstream_proxy

    try:
        start_agent(
            agent_type,
            llm_provider,
            model,
            max_steps,
            session_id=session_id,
            reasoning_effort=reasoning_effort,
            sandbox_env_file=sandbox_env_file,
            sandbox_keep_container=sandbox_keep_container,
            sandbox_cpus=sandbox_cpus,
            sandbox_memory=sandbox_memory,
            sandbox_offline_sdk_wheels=sandbox_offline_sdk_wheels,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
