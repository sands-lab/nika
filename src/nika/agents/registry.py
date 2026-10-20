"""Agent type registry used by ``nika agent run``.

Every agent is described by one :class:`AgentSpec`. Built-in agents are listed
in :data:`BUILTIN_AGENTS`; agents developed outside NIKA are resolved by
:mod:`nika.agents.plugins` (``nika agent add`` or a ``module:Class`` import path).
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from nika.agents.submission import ReportMapper

ALL_PROVIDERS = frozenset({"openai", "anthropic", "deepseek", "custom"})


@dataclass(frozen=True)
class AgentSpec:
    """Everything NIKA needs to know to run an agent.

    ``class_path`` is a ``module:Class`` path, imported only inside the
    agent's sandbox. The class receives the keyword arguments it declares
    among ``session_id``, ``model``, ``llm_provider``, ``max_steps``,
    ``reasoning_effort``, ``max_tokens``, and ``stream_output``.
    """

    id: str
    class_path: str
    providers: frozenset[str] = ALL_PROVIDERS
    # Which client family receives deepseek/custom credentials; ``provider``
    # follows the provider (Anthropic clients for ``anthropic``, else OpenAI).
    credential_family: Literal["openai", "anthropic", "provider"] = "openai"
    requires_provider: bool = True
    supports_max_tokens: bool = True
    # Model used when neither the CLI nor agent.model sets one.
    default_model: str | None = None
    # Sandbox template (``codex``, ``claude``, ``shell``); ``None`` runs the
    # agent in the NIKA process (test-only agents that need host state).
    template: str | None = "shell"
    # Frozen Python packages installed into the sandbox (see
    # :mod:`nika.agents.sandbox.wheels`), then extra pip requirements of an
    # added agent, installed from PyPI.
    requirements: str = "runtime"
    packages: tuple[str, ...] = ()
    # Extra hosts the sandbox may reach (an added agent's LLM endpoint).
    egress: tuple[str, ...] = ()
    # builtin, user (nika agent add), or import path.
    source: str = "builtin"
    # Added project directory, copied into the sandbox next to the agent package.
    import_root: str | None = None

    def client_family(self, provider: str | None) -> Literal["openai", "anthropic"]:
        """Model client family that receives *provider*'s credentials."""
        if self.credential_family == "provider":
            return "anthropic" if provider == "anthropic" else "openai"
        return self.credential_family

    def load_class(self) -> Any:
        """Import the agent class in this process (host-run agents only)."""
        module_name, _, attr = self.class_path.partition(":")
        try:
            module = importlib.import_module(module_name)
            return getattr(module, attr)
        except (ImportError, AttributeError) as exc:
            raise ValueError(
                f"Cannot load agent {self.id!r} from {self.class_path!r}: {exc}."
            ) from exc


BUILTIN_AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec(
        id="byo.langgraph",
        class_path="agent.byo.langgraph.react_agent:BasicReActAgent",
        credential_family="provider",
        requirements="langgraph",
    ),
    AgentSpec(
        id="byo.mcp_agent",
        class_path="agent.byo.mcp_agent.agent:McpAgent",
        credential_family="provider",
        requirements="mcp_agent",
    ),
    AgentSpec(
        id="byo.autogen",
        class_path="agent.byo.autogen.agent:AutogenAgent",
        credential_family="provider",
        requirements="autogen",
    ),
    AgentSpec(
        id="cli.codex",
        class_path="agent.cli.codex.agent:CodexCliAgent",
        providers=frozenset({"openai", "deepseek", "custom"}),
        supports_max_tokens=False,
        template="codex",
    ),
    AgentSpec(
        id="cli.claude",
        class_path="agent.cli.claude.agent:ClaudeAgent",
        providers=frozenset({"anthropic", "deepseek", "custom"}),
        credential_family="anthropic",
        template="claude",
    ),
    AgentSpec(
        id="sdk.codex_sdk",
        class_path="agent.sdk.codex_sdk.agent:CodexSdkAgent",
        providers=frozenset({"openai", "deepseek", "custom"}),
        supports_max_tokens=False,
        requirements="codex_sdk",
    ),
    AgentSpec(
        id="sdk.claude_sdk",
        class_path="agent.sdk.claude_sdk.agent:ClaudeSdkAgent",
        providers=frozenset({"anthropic", "deepseek", "custom"}),
        credential_family="anthropic",
        requirements="claude_sdk",
    ),
    AgentSpec(
        id="community.sade",
        class_path="agent.community.sade.agent:SadeAgent",
        providers=frozenset({"anthropic", "deepseek", "custom"}),
        credential_family="anthropic",
        supports_max_tokens=False,
        requirements="claude_sdk",
    ),
    AgentSpec(
        id="mock",
        class_path="nika.agents.mock.mock_agent:MockAgent",
        requires_provider=False,
        supports_max_tokens=False,
        default_model="mock",
        template=None,
    ),
)

_BUILTINS_BY_ID = {spec.id: spec for spec in BUILTIN_AGENTS}


def find_agent_spec(agent_type: str) -> AgentSpec | None:
    """Return the spec for *agent_type*, or ``None`` when it is unknown.

    Built-in IDs win, then agents registered with ``nika agent add``, then
    ``module:Class`` import paths.
    """
    name = agent_type.strip()
    if spec := _BUILTINS_BY_ID.get(name.lower()):
        return spec
    from nika.agents.plugins import find_external_agent_spec

    return find_external_agent_spec(name)


def get_agent_spec(agent_type: str) -> AgentSpec:
    if spec := find_agent_spec(agent_type):
        return spec
    raise ValueError(f"Unsupported agent type: {agent_type!r}")


class AgentTimeoutError(RuntimeError):
    """The agent run exceeded ``agent.timeout_sec``."""


async def run_pipeline(
    agent: Any, task_description: str, *, report_mapper: ReportMapper | None = None
) -> None:
    """Diagnosis, freeze and phase advance, then submission, for a host-run agent."""
    from agent.base import run_submission_phase
    from nika.agents.mcp_config import begin_submission_mcp_phase

    report = await agent.run_diagnosis(task_description)
    context = begin_submission_mcp_phase(agent.session_id, report)
    if report_mapper is None:
        await agent.run_submission(report, context)
        return
    await run_submission_phase(
        lambda: report_mapper.submit(
            report,
            context,
            session_id=agent.session_id,
            scenario_name=agent.scenario_name,
            trace_dir=agent.trace_dir,
        ),
        trace_dir=agent.trace_dir,
        stream_output=agent.stream_output,
        **report_mapper.trace_fields(),
    )


def run_agent(
    agent: Any,
    task_description: str,
    *,
    timeout_sec: int,
    report_mapper: ReportMapper | None = None,
) -> None:
    """Run a host-run agent's pipeline within the ``agent.timeout_sec`` budget.

    ``timeout_sec <= 0`` disables the budget.
    """

    async def _run() -> None:
        pipeline = run_pipeline(agent, task_description, report_mapper=report_mapper)
        if timeout_sec <= 0:
            return await pipeline
        try:
            async with asyncio.timeout(timeout_sec) as budget:
                return await pipeline
        except TimeoutError:
            # A TimeoutError raised by the agent itself (e.g. an LLM request)
            # is not a budget expiry; keep it for outcome classification.
            if not budget.expired():
                raise
        # Raised outside the handler so the chain carries no TimeoutError, which
        # outcome classification would read as an LLM endpoint timeout.
        raise AgentTimeoutError(
            f"agent run exceeded agent.timeout_sec ({timeout_sec}s)"
        )

    asyncio.run(_run())


def create_agent(
    agent_type: str,
    *,
    session_id: str,
    model: str,
    llm_provider: str | None = None,
    max_steps: int = 20,
    reasoning_effort: str | None = None,
    max_tokens: int | None = None,
    stream_output: bool = True,
) -> Any:
    """Instantiate a host-run agent (``template=None``) in this process."""
    spec = get_agent_spec(agent_type)
    if spec.template is not None:
        raise RuntimeError(f"Agent {agent_type!r} runs only inside its sandbox")
    if spec.requires_provider and not llm_provider:
        raise ValueError(
            f"{agent_type} requires an LLM provider: set agent.provider in "
            "config/nika.yaml or pass -p/--provider."
        )

    agent_class = spec.load_class()
    settings = {
        "session_id": session_id,
        "model": model,
        "llm_provider": llm_provider,
        "max_steps": max_steps,
        "reasoning_effort": reasoning_effort,
        "max_tokens": max_tokens,
        "stream_output": stream_output,
    }
    params = inspect.signature(agent_class).parameters
    if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        settings = {key: value for key, value in settings.items() if key in params}
    return agent_class(**settings)
