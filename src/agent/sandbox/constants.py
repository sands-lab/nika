"""Shared sandbox constants."""

from agent.registry import BUILTIN_AGENTS

MANIFEST_FILENAME = "sandbox_manifest.json"
RUN_FILENAME = "run.json"
RUNTIME_ENV_FILENAME = ".sandbox_runtime_env.json"
# In-sandbox SDK runner hand-off files (see ``agent.sandbox.runner``).
DIAGNOSIS_REPORT_FILENAME = "diagnosis_report.json"
SUBMISSION_CONTEXT_FILENAME = "submission_context.json"
# Credential routing keys from ``SbxCredentialPlan.sentinel_runtime_env``.
CREDENTIAL_ENV_KEYS = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
    }
)
# Prefix of model proxy tokens (``agent.sandbox.model_proxy``).
PROXY_TOKEN_PREFIX = "nika-proxy-"
# Agents that run the in-sandbox Python runner (``agent.sandbox.runner``).
SDK_AGENT_TYPES = frozenset(
    spec.id for spec in BUILTIN_AGENTS if spec.sbx is not None and spec.sbx.sdk
)
