"""Agent type registry used by ``nika agent run``.

Every agent is described by one :class:`AgentSpec`. Built-in agents are listed
in :data:`BUILTIN_AGENTS`; agents developed outside NIKA are resolved by
:mod:`agent.plugins` (``nika agent add``, ``nika.agents`` entry points, or a
``module:Class`` import path).
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import os
from dataclasses import dataclass, field
from typing import Any, Literal

from agent.sandbox.config import ENV_SANDBOX_EXECUTION

ALL_PROVIDERS = frozenset({"openai", "anthropic", "deepseek", "custom"})


@dataclass(frozen=True)
class SbxWiring:
    """How a built-in agent runs in Docker Sandboxes.

    ``native`` is the sbx agent template (``codex``, ``claude``, ``shell``).
    ``sdk`` agents run NIKA's in-sandbox Python runner in a ``shell`` sandbox.
    """

    native: str
    sdk: bool = False


@dataclass(frozen=True)
class AgentSpec:
    """Everything NIKA needs to know to run an agent.

    ``factory`` is a ``module:attribute`` path, imported only when the agent
    runs. The factory receives the keyword arguments it declares among
    ``session_id``, ``model``, ``llm_provider``, ``max_steps``,
    ``reasoning_effort``, ``max_tokens``, and ``stream_output``.
    """

    id: str
    factory: str
    providers: frozenset[str] = ALL_PROVIDERS
    # Which client family receives deepseek/custom credentials.
    credential_family: Literal["openai", "anthropic"] = "openai"
    requires_provider: bool = True
    supports_max_tokens: bool = True
    # Model used when neither the CLI nor agent.model sets one.
    default_model: str | None = None
    sbx: SbxWiring | None = None
    # builtin, user (nika agent add), entry point, or import path.
    source: str = "builtin"
    # Declared sandbox definition of an external agent (validated, not executed yet).
    sandbox_definition: dict[str, Any] | None = field(default=None, compare=False)

    def load_factory(self) -> Any:
        module_name, _, attr = self.factory.partition(":")
        try:
            module = importlib.import_module(module_name)
            return getattr(module, attr)
        except (ImportError, AttributeError) as exc:
            hint = (
                " If `uv sync` removed the package, run `nika agent add` again."
                if self.source == "user"
                else ""
            )
            raise ValueError(
                f"Cannot load agent {self.id!r} from {self.factory!r}: {exc}.{hint}"
            ) from exc


BUILTIN_AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec(
        id="byo.langgraph",
        factory="agent.byo.langgraph.react_agent:BasicReActAgent",
    ),
    AgentSpec(id="byo.mcp_agent", factory="agent.byo.mcp_agent.agent:McpAgent"),
    AgentSpec(id="byo.autogen", factory="agent.byo.autogen.agent:AutogenAgent"),
    AgentSpec(
        id="cli.codex",
        factory="agent.cli.codex.agent:CodexCliAgent",
        providers=frozenset({"openai", "deepseek", "custom"}),
        supports_max_tokens=False,
        sbx=SbxWiring(native="codex"),
    ),
    AgentSpec(
        id="cli.claude",
        factory="agent.cli.claude.agent:ClaudeAgent",
        providers=frozenset({"anthropic", "deepseek", "custom"}),
        credential_family="anthropic",
        sbx=SbxWiring(native="claude"),
    ),
    AgentSpec(
        id="sdk.codex_sdk",
        factory="agent.sdk.codex_sdk.agent:CodexSdkAgent",
        providers=frozenset({"openai", "deepseek", "custom"}),
        supports_max_tokens=False,
        sbx=SbxWiring(native="shell", sdk=True),
    ),
    AgentSpec(
        id="sdk.claude_sdk",
        factory="agent.sdk.claude_sdk.agent:ClaudeSdkAgent",
        providers=frozenset({"anthropic", "deepseek", "custom"}),
        credential_family="anthropic",
        sbx=SbxWiring(native="shell", sdk=True),
    ),
    AgentSpec(
        id="community.sade",
        factory="agent.community.sade.agent:SadeAgent",
        providers=frozenset({"anthropic", "deepseek", "custom"}),
        credential_family="anthropic",
        supports_max_tokens=False,
        sbx=SbxWiring(native="shell", sdk=True),
    ),
    AgentSpec(
        id="mock",
        factory="agent.mock.mock_agent:MockAgent",
        requires_provider=False,
        supports_max_tokens=False,
        default_model="mock",
    ),
)

_BUILTINS_BY_ID = {spec.id: spec for spec in BUILTIN_AGENTS}

SANDBOX_AGENT_TYPES = frozenset(
    spec.id for spec in BUILTIN_AGENTS if spec.sbx is not None
)


def find_agent_spec(agent_type: str) -> AgentSpec | None:
    """Return the spec for *agent_type*, or ``None`` when it is unknown.

    Built-in IDs win, then agents registered with ``nika agent add``, then
    ``nika.agents`` entry points, then ``module:Class`` import paths.
    """
    name = agent_type.strip()
    if spec := _BUILTINS_BY_ID.get(name.lower()):
        return spec
    from agent.plugins import find_external_agent_spec

    return find_external_agent_spec(name)


def get_agent_spec(agent_type: str) -> AgentSpec:
    if spec := find_agent_spec(agent_type):
        return spec
    raise ValueError(f"Unsupported agent type: {agent_type!r}")


class AgentTimeoutError(RuntimeError):
    """The agent run exceeded ``agent.timeout_sec``."""


def run_agent(agent: Any, task_description: str, *, timeout_sec: int) -> Any:
    """Run ``agent.run`` to completion within the shared ``agent.timeout_sec`` budget.

    ``timeout_sec <= 0`` disables the budget.
    """

    async def _run() -> Any:
        if timeout_sec <= 0:
            return await agent.run(task_description=task_description)
        try:
            async with asyncio.timeout(timeout_sec) as budget:
                return await agent.run(task_description=task_description)
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

    return asyncio.run(_run())


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
    """Instantiate an agent for ``agent_type``."""
    spec = get_agent_spec(agent_type)
    if spec.sbx is not None and os.environ.get(ENV_SANDBOX_EXECUTION) != "1":
        raise RuntimeError(
            f"Agent {agent_type!r} can only run inside the Docker sandbox"
        )

    if spec.requires_provider and not llm_provider:
        raise ValueError(
            f"{agent_type} requires an LLM provider: set agent.provider in "
            "config/nika.yaml or pass -p/--provider."
        )

    factory = spec.load_factory()
    settings = {
        "session_id": session_id,
        "model": model,
        "llm_provider": llm_provider,
        "max_steps": max_steps,
        "reasoning_effort": reasoning_effort,
        "max_tokens": max_tokens,
        "stream_output": stream_output,
    }
    params = inspect.signature(factory).parameters
    if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        settings = {key: value for key, value in settings.items() if key in params}
    return factory(**settings)
