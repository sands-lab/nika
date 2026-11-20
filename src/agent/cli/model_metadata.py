"""Read serving limits through a CLI agent's authenticated model proxy."""

from __future__ import annotations

import json
import urllib.request


def model_context_window(base_url: str, model: str, api_key: str) -> int | None:
    """Return the advertised context limit, or None when it is unknown."""
    url = base_url.strip().rstrip("/")
    if not url:
        return None
    if not url.endswith("/v1"):
        url += "/v1"
    try:
        request = urllib.request.Request(f"{url}/models")
        if api_key:
            request.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(request, timeout=10) as response:
            entries = json.load(response).get("data") or []
    except (OSError, ValueError, AttributeError):
        return None
    for entry in entries:
        if isinstance(entry, dict) and model in (entry.get("id"), entry.get("root")):
            window = entry.get("max_model_len")
            if type(window) is int and window > 0:
                return window
    return None
