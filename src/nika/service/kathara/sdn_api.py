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


class SdnAPIMixin:
    """Operator-style SDN access: ONOS REST and OVS CLI on switches."""

    def sdn_onos_rest(self: _SupportsBase, path: str) -> dict[str, Any]:
        """GET an ONOS REST path via fabric_mgr (operator-style curl)."""
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
        return {
            "onos_oob": f"{ONOS_OOB_IP}:{ONOS_REST_PORT}",
            "fabric_mgr_oob": FABRIC_MGR_OOB_IP,
            "path": normalized,
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
