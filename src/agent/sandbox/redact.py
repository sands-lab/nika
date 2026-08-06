"""Redact secrets from logs and command strings."""

from __future__ import annotations

import re

_SENSITIVE_ENV_KEYS = frozenset(
    {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "DEEPSEEK_API_KEY",
        "NIKA_CUSTOM_API_KEY",
        "CUSTOM_API_KEY",
        "LANGFUSE_SECRET_KEY",
        "NIKA_REMOTE_TOKEN",
    }
)

_SECRET_PATTERNS = (
    re.compile(r"(sk-[A-Za-z0-9_-]{10,})"),
    re.compile(r"(sk-ant-[A-Za-z0-9_-]{10,})"),
    re.compile(r'((?:OPENAI|ANTHROPIC|DEEPSEEK)_[A-Z_]*=)[^\s"\']+'),
)


def redact_env_value(key: str, value: str) -> str:
    if key in _SENSITIVE_ENV_KEYS or "TOKEN" in key or "SECRET" in key or "KEY" in key:
        if not value:
            return value
        return "***REDACTED***"
    return value


def redact_text(text: str) -> str:
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(
            lambda m: (
                m.group(1) + "***REDACTED***"
                if m.lastindex and m.lastindex >= 1 and "=" in m.group(0)
                else "***REDACTED***"
            ),
            redacted,
        )
    return redacted


def redact_env_dict(env: dict[str, str]) -> dict[str, str]:
    return {key: redact_env_value(key, value) for key, value in env.items()}
