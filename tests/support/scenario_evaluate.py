"""Test-path scenario behavioral evaluation.

Production startup uses fast ``startup_verify_lab``. Full baseline checks live
here as ``evaluate_scenario`` (unified API over ``verify_lab``).
"""

from __future__ import annotations

import time
from typing import Any

from nika.net_env.base import NetworkEnvBase

_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_RETRY_DELAY_SEC = 5.0


def evaluate_scenario(
    net_env: NetworkEnvBase,
    *,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    retry_delay_sec: float = _DEFAULT_RETRY_DELAY_SEC,
) -> tuple[bool, dict[str, Any]]:
    """Confirm expected healthy network behavior after deploy (tests only)."""
    last: dict[str, Any] = {"skipped": True, "reason": "no_verify_lab"}
    ok = True
    for attempt in range(max_attempts):
        result = net_env.verify_lab()
        if result is None:
            return True, {"skipped": True, "reason": "no_verify_lab"}
        checks = result.get("checks") or {}
        ok = bool(result.get("verified")) and all(checks.values())
        last = result
        if ok:
            return ok, result
        if attempt + 1 < max_attempts:
            time.sleep(retry_delay_sec)
    return ok, last
