"""BMv2 / P4Runtime operator helpers for Kathara P4 scenarios."""

from __future__ import annotations

import json
from typing import Any

from nika.service.kathara.base_api import KatharaBaseAPI, _SupportsBase

_P4RT_BASE = (
    "python3 /opt/nika/p4rt_manager.py "
    "--intent /tmp/p4_fabric/intent.json "
    "--p4info /tmp/p4_fabric/fabric.p4info.txt "
    "--json /tmp/p4_fabric/fabric.json"
)


def _sanitize_p4rt_payload(payload: Any) -> Any:
    """Drop private post-counter fault registers/tables from agent-facing output."""
    if isinstance(payload, dict):
        cleaned: dict[str, Any] = {}
        for key, value in payload.items():
            key_s = str(key)
            if "internal_fault" in key_s:
                continue
            cleaned[key_s] = _sanitize_p4rt_payload(value)
        return cleaned
    if isinstance(payload, list):
        return [_sanitize_p4rt_payload(item) for item in payload]
    return payload


class BMv2APIMixin:
    """Run p4rt_manager on fabric_mgr with private-state sanitization."""

    def p4rt_exec(self: _SupportsBase, args: str, timeout: float = 90) -> str:
        """Run ``p4rt_manager.py`` with *args* on fabric_mgr; sanitize JSON output."""
        command = f"{_P4RT_BASE} {args}".strip()
        raw = self.exec_cmd("fabric_mgr", command, timeout=timeout)
        start = raw.find("{")
        if start < 0:
            return raw
        try:
            payload = json.loads(raw[start:])
        except json.JSONDecodeError:
            return raw
        return json.dumps(_sanitize_p4rt_payload(payload), indent=2, default=str)


class KatharaBMv2API(KatharaBaseAPI, BMv2APIMixin):
    """Kathara API for live BMv2 P4Runtime access."""

    pass
