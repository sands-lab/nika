"""Shared sandbox constants."""

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
