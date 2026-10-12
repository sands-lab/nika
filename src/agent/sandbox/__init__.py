"""Sandboxed execution for NIKA troubleshooting agents."""

__all__ = [
    "SANDBOX_SUPPORTED_AGENTS",
    "SandboxConfig",
    "SandboxManager",
    "resolve_sandbox_config",
]


def __getattr__(name: str):
    if name == "SANDBOX_SUPPORTED_AGENTS":
        # Lazy: agent.registry imports agent.sandbox.config.
        from agent.registry import SANDBOX_AGENT_TYPES

        return SANDBOX_AGENT_TYPES
    if name == "SandboxConfig":
        from agent.sandbox.config import SandboxConfig

        return SandboxConfig
    if name == "resolve_sandbox_config":
        from agent.sandbox.config import resolve_sandbox_config

        return resolve_sandbox_config
    if name == "SandboxManager":
        from agent.sandbox.manager import SandboxManager

        return SandboxManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
