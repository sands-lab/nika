"""ONOS + OVS evidence collectors for sdn_l3_clos (no ground-truth verdicts)."""

from __future__ import annotations

import json
from typing import Any, Literal

from nika.net_env.kathara.sdn.topology_model import (
    FABRIC_MGR_OOB_IP,
    ONOS_OOB_IP,
    ONOS_REST_PORT,
    TopoSize,
    build_clos_fabric_model,
    device_id,
    dpid_for_leaf,
    dpid_for_spine,
)
from nika.service.kathara.base_api import KatharaBaseAPI, _SupportsBase

_ONOS_AUTH = "onos:rocks"
TopoSizeArg = Literal["s", "m", "l"]


class SdnAPIMixin:
    """Read-only SDN fabric evidence for agents."""

    def _sdn_exec(
        self: _SupportsBase, host: str, command: str, timeout: float = 15
    ) -> str:
        return self.exec_cmd(host, command, timeout=timeout)

    def _onos_curl(self: _SupportsBase, path: str) -> str:
        url = f"http://{ONOS_OOB_IP}:{ONOS_REST_PORT}{path}"
        return self._sdn_exec(
            "fabric_mgr",
            f"curl -s -u {_ONOS_AUTH} --connect-timeout 3 '{url}' || echo '{{}}'",
            timeout=20,
        )

    def _parse_json(self, raw: str) -> Any:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}

    def list_ovs_switches(self: _SupportsBase) -> list[str]:
        names = []
        for name, machine in (self.lab.machines or {}).items():
            image = machine.get_image() if hasattr(machine, "get_image") else ""
            if name.startswith(("leaf_", "spine_")) or "sdn" in str(image):
                if name not in ("onos", "fabric_mgr"):
                    names.append(name)
        return sorted(set(names))

    def sdn_onos_topology(self: _SupportsBase) -> dict[str, Any]:
        """ONOS device / link / host inventory (evidence only)."""
        return {
            "onos_oob": f"{ONOS_OOB_IP}:{ONOS_REST_PORT}",
            "fabric_mgr_oob": FABRIC_MGR_OOB_IP,
            "devices": self._parse_json(self._onos_curl("/onos/v1/devices")),
            "links": self._parse_json(self._onos_curl("/onos/v1/links")),
            "hosts": self._parse_json(self._onos_curl("/onos/v1/hosts")),
        }

    def sdn_controller_logs(self: _SupportsBase, rows: int = 80) -> dict[str, Any]:
        rows = max(1, min(int(rows), 500))
        log = self._sdn_exec(
            "onos",
            f"tail -n {rows} /root/onos/apache-karaf-*/data/log/karaf.log 2>/dev/null || "
            f"tail -n {rows} /root/onos/log/karaf.log 2>/dev/null || "
            f"tail -n {rows} /var/log/onos/*.log 2>/dev/null || "
            "echo ''",
            timeout=20,
        )
        return {"host": "onos", "rows": rows, "log": log}

    def sdn_controller_apps(self: _SupportsBase) -> dict[str, Any]:
        """ONOS application activation state (evidence only)."""
        return {
            "applications": self._parse_json(self._onos_curl("/onos/v1/applications")),
        }

    def sdn_onos_flows(self: _SupportsBase) -> dict[str, Any]:
        """Live ONOS flow store (controller desired/installed state)."""
        return {
            "source": "onos_rest",
            "plane": "controller_live",
            "flows": self._parse_json(self._onos_curl("/onos/v1/flows")),
        }

    def sdn_onos_groups(self: _SupportsBase) -> dict[str, Any]:
        """Live ONOS group store (controller desired/installed state)."""
        return {
            "source": "onos_rest",
            "plane": "controller_live",
            "groups": self._parse_json(self._onos_curl("/onos/v1/groups")),
        }

    def sdn_programmed_forwarding_rules(
        self: _SupportsBase, topo_size: TopoSizeArg = "s"
    ) -> dict[str, Any]:
        """Live ONOS forwarding state (flows + groups from the controller).

        Prefer this over host-side model rebuild when diagnosing. Optional
        ``topo_size`` is retained for callers that still filter by Clos size.
        """
        size: TopoSize = topo_size if topo_size in ("s", "m", "l") else "s"
        flows_payload = self.sdn_onos_flows()["flows"]
        groups_payload = self.sdn_onos_groups()["groups"]
        flows = (
            flows_payload.get("flows", []) if isinstance(flows_payload, dict) else []
        )
        groups = (
            groups_payload.get("groups", []) if isinstance(groups_payload, dict) else []
        )
        return {
            "source": "onos_live",
            "topo_size": size,
            "flows": flows,
            "groups": groups,
            "expected_devices": build_clos_fabric_model(size).expected_device_ids(),
        }

    def sdn_ovs_flows(self: _SupportsBase, switch_name: str) -> dict[str, Any]:
        out = self._sdn_exec(
            switch_name,
            f"ovs-ofctl -O OpenFlow13 dump-flows {switch_name} 2>/dev/null || true",
        )
        return {"switch": switch_name, "plane": "switch_observed", "flows": out}

    def sdn_ovs_groups(self: _SupportsBase, switch_name: str) -> dict[str, Any]:
        out = self._sdn_exec(
            switch_name,
            f"ovs-ofctl -O OpenFlow13 dump-groups {switch_name} 2>/dev/null || true",
        )
        return {"switch": switch_name, "plane": "switch_observed", "groups": out}

    def sdn_ovs_port_counters(self: _SupportsBase, switch_name: str) -> dict[str, Any]:
        out = self._sdn_exec(
            switch_name,
            f"ovs-ofctl -O OpenFlow13 dump-ports {switch_name} 2>/dev/null || true",
        )
        return {"switch": switch_name, "plane": "switch_observed", "ports": out}

    def sdn_ovs_status(self: _SupportsBase, switch_name: str) -> dict[str, Any]:
        show = self._sdn_exec(switch_name, "ovs-vsctl show 2>/dev/null || true")
        of_show = self._sdn_exec(
            switch_name,
            f"ovs-ofctl -O OpenFlow13 show {switch_name} 2>/dev/null || true",
        )
        controller = self._sdn_exec(
            switch_name,
            f"ovs-vsctl get-controller {switch_name} 2>/dev/null || true",
        )
        return {
            "switch": switch_name,
            "plane": "switch_observed",
            "ovs_vsctl_show": show,
            "of_port_desc": of_show,
            "controllers": controller.strip(),
        }

    def sdn_endpoint_reachability(
        self: _SupportsBase,
        source: str,
        target_ip: str,
        count: int = 3,
    ) -> dict[str, Any]:
        count = max(1, min(int(count), 10))
        out = self._sdn_exec(
            source,
            f"ping -c {count} -W 2 {target_ip} 2>&1 || true",
            timeout=30,
        )
        return {
            "source": source,
            "target_ip": target_ip,
            "count": count,
            "ping_output": out,
        }

    def sdn_get_fabric_state(
        self: _SupportsBase,
        topo_size: TopoSizeArg = "s",
        switch_name: str | None = None,
        source: str | None = None,
        target_ip: str | None = None,
        log_rows: int = 0,
    ) -> dict[str, Any]:
        """Aggregate SDN fabric evidence (routing_state-style).

        Controller section is live ONOS flow/group store; switch section is
        OVS-observed. Optional ``switch_name`` focuses dataplane dumps and
        filters controller flows/groups to that device id when resolvable.
        """
        switches = [switch_name] if switch_name else self.list_ovs_switches()[:4]
        intent = self.sdn_programmed_forwarding_rules(topo_size)
        if switch_name:
            device_ids: set[str] = set()
            if switch_name.startswith("leaf_"):
                leaf_id = int(switch_name.split("_", 1)[1])
                device_ids.add(device_id(dpid_for_leaf(leaf_id)))
            elif switch_name.startswith("spine_"):
                spine_id = int(switch_name.split("_", 1)[1])
                device_ids.add(device_id(dpid_for_spine(spine_id)))
            if device_ids:
                intent = {
                    **intent,
                    "flows": [
                        f for f in intent["flows"] if f.get("deviceId") in device_ids
                    ],
                    "groups": [
                        g for g in intent["groups"] if g.get("deviceId") in device_ids
                    ],
                    "filter_device_ids": sorted(device_ids),
                }

        observed: dict[str, Any] = {}
        for sw in switches:
            observed[sw] = {
                "flows": self.sdn_ovs_flows(sw)["flows"],
                "groups": self.sdn_ovs_groups(sw)["groups"],
                "port_counters": self.sdn_ovs_port_counters(sw)["ports"],
                "ovs_status": self.sdn_ovs_status(sw),
            }

        payload: dict[str, Any] = {
            "onos_topology": self.sdn_onos_topology(),
            "controller_apps": self.sdn_controller_apps(),
            "controller_live_state": intent,
            # Compat alias for existing tests/tools.
            "controller_programmed_intent": intent,
            "switch_observed_state": observed,
        }
        if log_rows and log_rows > 0:
            payload["controller_logs"] = self.sdn_controller_logs(rows=log_rows)
        if source and target_ip:
            payload["endpoint_reachability"] = self.sdn_endpoint_reachability(
                source, target_ip
            )
        return payload


class KatharaSdnAPI(KatharaBaseAPI, SdnAPIMixin):
    """Kathara API surface for SDN Clos fabric evidence."""

    pass
