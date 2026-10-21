"""Sandboxed execution for troubleshooting agents."""

__all__ = [
    "SandboxConfig",
    "SandboxManager",
    "resolve_sandbox_config",
]


def __getattr__(name: str):
    if name == "SandboxConfig":
        from nika.agents.sandbox.config import SandboxConfig

        return SandboxConfig
    if name == "resolve_sandbox_config":
        from nika.agents.sandbox.config import resolve_sandbox_config

        return resolve_sandbox_config
    if name == "SandboxManager":
        from nika.agents.sandbox.manager import SandboxManager

        return SandboxManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
