"""ONOS + OVS CLI/RPC helpers for sdn_l3_clos (no ground-truth verdicts)."""

from __future__ import annotations

import json
import shlex
from typing import Any

from nika.net_env.sdn_l3_clos.topology_model import (
    FABRIC_MGR_OOB_IP,
    ONOS_OOB_IP,
    ONOS_REST_PORT,
)
from nika.service.kathara.base_api import KatharaBaseAPI, _SupportsBase

_ONOS_AUTH = "onos:rocks"


def _item_devices(item: Any) -> set[str]:
    """Device ids an ONOS list item refers to (device, flow, port, link, host)."""
    if not isinstance(item, dict):
        return set()
    found = {
        str(item[key]) for key in ("deviceId", "elementId", "device") if key in item
    }
    if str(item.get("id", "")).startswith("of:"):
        found.add(str(item["id"]))
    for end in ("src", "dst"):
        if isinstance(item.get(end), dict) and "device" in item[end]:
            found.add(str(item[end]["device"]))
    for location in item.get("locations") or []:
        if isinstance(location, dict) and "elementId" in location:
            found.add(str(location["elementId"]))
    return found


def onos_summary(body: Any, device: str | None = None) -> tuple[dict[str, Any], Any]:
    """Return (compact summary, body narrowed to ``device``) for an ONOS reply."""
    if not isinstance(body, dict):
        return {}, body
    summary: dict[str, Any] = {}
    narrowed: dict[str, Any] = {}
    for key, value in body.items():
        if not isinstance(value, list):
            narrowed[key] = value
            continue
        if device:
            value = [item for item in value if device in _item_devices(item)]
        narrowed[key] = value
        row: dict[str, Any] = {"count": len(value)}
        per_device: dict[str, int] = {}
        states: dict[str, int] = {}
        for item in value:
            for dev in _item_devices(item):
                per_device[dev] = per_device.get(dev, 0) + 1
            if isinstance(item, dict):
                for field in ("state", "available"):
                    if field in item:
                        label = f"{field}={item[field]}"
                        states[label] = states.get(label, 0) + 1
        if per_device:
            row["per_device"] = dict(sorted(per_device.items()))
        if states:
            row["states"] = dict(sorted(states.items()))
        summary[key] = row
    return summary, narrowed


class SdnAPIMixin:
    """Operator-style SDN access: ONOS REST and OVS CLI on switches."""

    def sdn_onos_rest(
        self: _SupportsBase, path: str, device: str | None = None
    ) -> dict[str, Any]:
        """GET an ONOS REST path via fabric_mgr (operator-style curl).

        The reply leads with a compact summary (item counts, per-device counts,
        states); ``device`` keeps only items that belong to that device id.
        """
        normalized = path if path.startswith("/") else f"/{path}"
        url = f"http://{ONOS_OOB_IP}:{ONOS_REST_PORT}{normalized}"
        raw = self.exec_cmd(
            "fabric_mgr",
            f"curl -s -u {_ONOS_AUTH} --connect-timeout 3 {shlex.quote(url)} "
            "|| echo '{}'",
            timeout=20,
        )
        try:
            body: Any = json.loads(raw)
        except json.JSONDecodeError:
            body = {"raw": raw}
        summary, body = onos_summary(body, device)
        return {
            "path": normalized,
            "device_filter": device,
            "summary": summary,
            "onos_oob": f"{ONOS_OOB_IP}:{ONOS_REST_PORT}",
            "fabric_mgr_oob": FABRIC_MGR_OOB_IP,
            "body": body,
        }

    def sdn_ovs_exec(
        self: _SupportsBase, switch_name: str, command: str, timeout: float = 15
    ) -> dict[str, Any]:
        """Run an OVS/OpenFlow CLI command on a switch (e.g. ovs-ofctl / ovs-vsctl)."""
        out = self.exec_cmd(switch_name, command, timeout=timeout)
        return {"switch": switch_name, "command": command, "output": out}

    def sdn_controller_logs(self: _SupportsBase, rows: int = 80) -> dict[str, Any]:
        rows = max(1, min(int(rows), 500))
        log = self.exec_cmd(
            "onos",
            f"tail -n {rows} /root/onos/apache-karaf-*/data/log/karaf.log 2>/dev/null || "
            f"tail -n {rows} /root/onos/log/karaf.log 2>/dev/null || "
            f"tail -n {rows} /var/log/onos/*.log 2>/dev/null || "
            "echo ''",
            timeout=20,
        )
        return {"host": "onos", "rows": rows, "log": log}


class KatharaSdnAPI(KatharaBaseAPI, SdnAPIMixin):
    """Kathara API surface for SDN Clos fabric evidence."""

    pass
