"""Subscription logins that NIKA's model proxy uses on the host.

On every sandbox runtime the model proxy adds the login to the agent's
requests; the sandbox only ever sees the per-session proxy token:

* Claude: ``CLAUDE_CODE_OAUTH_TOKEN`` in ``.env``, made with
  ``claude setup-token``. It is long-lived, so NIKA never refreshes it.
* Codex: a ChatGPT login in NIKA's own ``CODEX_HOME`` (:data:`CODEX_LOGIN_HOME`),
  made with ``codex login``. :class:`CodexLogin` refreshes its access token
  when it expires and writes the login back, as Codex does. Being separate
  from ``~/.codex``, the refresh never signs out the user's own Codex.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from nika.agents.sandbox.locks import host_file_lock
from agent.llm.endpoints import ENV_CLAUDE_CODE_OAUTH_TOKEN

CODEX_LOGIN_HOME = Path.home() / ".local" / "state" / "nika" / "codex"
# Where Codex sends model calls for ChatGPT logins.
CHATGPT_CODEX_URL = "https://chatgpt.com/backend-api/codex"
SUBSCRIPTION_DOCS = "docs/operations/agent-sandbox.md#subscriptions-with-e2b"
# Refresh this long before the access token expires.
_REFRESH_MARGIN_SEC = 300


def _jwt_claims(token: str) -> dict[str, Any]:
    """Claims of *token* (not verified: the issuer checks its own tokens)."""
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class CodexLogin:
    """The ChatGPT login in a Codex ``auth.json``, refreshed when it expires."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._tokens = self._read()["tokens"]

    @classmethod
    def load(cls, home: Path | None = None) -> CodexLogin | None:
        """The login in *home* (:data:`CODEX_LOGIN_HOME`), or None without one."""
        path = (home or CODEX_LOGIN_HOME) / "auth.json"
        try:
            return cls(path)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    @property
    def account_id(self) -> str:
        return self._tokens["account_id"]

    def access_token(self) -> str:
        """A valid access token, refreshing the login first when it expires."""
        with self._lock:
            if self._expiring(self._tokens):
                with host_file_lock(self.path.with_name("auth.json.lock")):
                    # Another NIKA process may have refreshed it meanwhile.
                    self._tokens = self._read()["tokens"]
                    if self._expiring(self._tokens):
                        self._refresh()
            return self._tokens["access_token"]

    def _read(self) -> dict[str, Any]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        tokens = data["tokens"]
        for key in ("access_token", "refresh_token", "id_token", "account_id"):
            if not isinstance(tokens.get(key), str) or not tokens[key]:
                raise ValueError(f"Codex login in {self.path} has no {key}")
        return data

    @staticmethod
    def _expiring(tokens: dict[str, Any]) -> bool:
        expires = _jwt_claims(tokens["access_token"]).get("exp", 0)
        return float(expires) - time.time() < _REFRESH_MARGIN_SEC

    def _refresh(self) -> None:
        # The ID token names the OAuth client and issuer this login belongs to.
        claims = _jwt_claims(self._tokens["id_token"])
        audience = claims.get("aud")
        client_id = audience[0] if isinstance(audience, list) else audience
        issuer = str(claims.get("iss") or "").rstrip("/")
        if not client_id or not issuer:
            raise RuntimeError(
                f"Cannot refresh the Codex login in {self.path}; log in again "
                f"(see {SUBSCRIPTION_DOCS})."
            )
        response = httpx.post(
            f"{issuer}/oauth/token",
            json={
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": self._tokens["refresh_token"],
                "scope": "openid profile email",
            },
            timeout=60,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Codex login refresh failed with HTTP {response.status_code}; "
                f"log in again (see {SUBSCRIPTION_DOCS})."
            )
        fresh = response.json()
        data = self._read()
        for key in ("access_token", "refresh_token", "id_token"):
            if fresh.get(key):
                data["tokens"][key] = fresh[key]
        data["last_refresh"] = datetime.now(timezone.utc).isoformat()
        tmp = self.path.with_name("auth.json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, self.path)
        self._tokens = data["tokens"]


def claude_oauth_token(sources: dict[str, str]) -> str:
    """The ``claude setup-token`` token in the process env or *sources* (.env)."""
    return (
        os.environ.get(ENV_CLAUDE_CODE_OAUTH_TOKEN, "").strip()
        or sources.get(ENV_CLAUDE_CODE_OAUTH_TOKEN, "").strip()
    )
