"""Docker sandbox execution for NIKA troubleshooting agents."""

SANDBOX_SUPPORTED_AGENTS = (
    "cli.codex",
    "cli.claude",
    "sdk.codex_sdk",
    "sdk.claude_sdk",
    "community.sade",
)

__all__ = [
    "SANDBOX_SUPPORTED_AGENTS",
    "SandboxConfig",
    "SbxSandboxManager",
    "resolve_sandbox_config",
    "sbx_available",
]


def __getattr__(name: str):
    if name == "SandboxConfig":
        from agent.sandbox.config import SandboxConfig

        return SandboxConfig
    if name == "resolve_sandbox_config":
        from agent.sandbox.config import resolve_sandbox_config

        return resolve_sandbox_config
    if name == "SbxSandboxManager":
        from agent.sandbox.sbx.manager import SbxSandboxManager

        return SbxSandboxManager
    if name == "sbx_available":
        from agent.sandbox.sbx.client import sbx_available

        return sbx_available
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
