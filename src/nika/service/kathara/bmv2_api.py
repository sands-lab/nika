import json

from nika.service.kathara.base_api import KatharaBaseAPI, _SupportsBase


class BMv2APIMixin:
    """Read live P4Runtime state from Kathara BMv2 scenarios."""

    @staticmethod
    def _active_values(values: dict | None) -> dict:
        return {
            str(key): value
            for key, value in (values or {}).items()
            if value and value != {"packets": 0, "bytes": 0}
        }

    def _port_statistics(self: _SupportsBase, switch_name: str) -> list[dict]:
        raw = self.exec_cmd(switch_name, "ip -j -s link show", timeout=20)
        try:
            links = json.loads(raw)
        except json.JSONDecodeError:
            return []
        ports = []
        for link in links if isinstance(links, list) else []:
            name = str(link.get("ifname", ""))
            if not name.startswith("eth"):
                continue
            stats = link.get("stats64") or link.get("stats") or {}
            ports.append(
                {
                    "name": name,
                    "state": link.get("operstate"),
                    "mtu": link.get("mtu"),
                    "rx": {
                        key: int((stats.get("rx") or {}).get(key, 0))
                        for key in ("packets", "bytes", "errors", "dropped")
                    },
                    "tx": {
                        key: int((stats.get("tx") or {}).get(key, 0))
                        for key in ("packets", "bytes", "errors", "dropped")
                    },
                }
            )
        return ports

    def p4_get_runtime_state(
        self: _SupportsBase, switch_name: str | None = None
    ) -> dict:
        """Return live pipeline, forwarding, counter, queue, flow, and ECN state."""
        focus_arg = f" --switch {switch_name}" if switch_name else ""
        command = (
            "python3 /opt/nika/p4rt_manager.py --intent /tmp/p4_fabric/intent.json "
            "--p4info /tmp/p4_fabric/fabric.p4info.txt "
            "--json /tmp/p4_fabric/fabric.json "
            f"read{focus_arg}"
        )
        raw = self.exec_cmd("fabric_mgr", command, timeout=90)
        try:
            start = raw.find("{")
            observed = json.loads(raw[start:]) if start >= 0 else {}
        except json.JSONDecodeError:
            observed = {"ok": False, "raw": raw[-2000:]}

        switches = observed.get("switches") or observed
        gateway_scenario = str(
            getattr(getattr(self, "lab", None), "name", "")
        ).startswith("p4_dc_gateway")
        payload: dict[str, dict] = {}
        for name, live in switches.items() if isinstance(switches, dict) else []:
            counters = {
                counter_name: self._active_values(values)
                for counter_name, values in (live.get("counters") or {}).items()
            }
            state = {
                "pipeline": live.get("pipeline"),
                "ipv4_lpm": live.get("ipv4_lpm", []),
                "action_selector_members": live.get("members", []),
                "action_selector_groups": live.get("groups", []),
                "counters": counters,
                "ports": self._port_statistics(name),
                "queue_statistics": {},
                "flow_tracking": {},
                "ecn": {},
            }
            if gateway_scenario:
                state["queue_statistics"] = {
                    "occupancy": self._active_values(
                        live.get("registers", {}).get("queue_occupancy", {})
                    )
                }
                state["flow_tracking"] = {
                    "estimator": "minimum_of_four_counting_bloom_positions",
                    "imbalance": "syn_estimate - non_syn_estimate",
                    "syn_packets": counters.get("flow_syn", {}),
                    "non_syn_packets": counters.get("flow_non_syn", {}),
                    "bloom_positions": "maintained in dataplane registers",
                }
                state["ecn"] = live.get("runtime_config", {}).get("ecn_config", [])
            payload[str(name)] = state
        return {"switches": payload}


class KatharaBMv2API(KatharaBaseAPI, BMv2APIMixin):
    """Kathara API for live BMv2 P4Runtime state."""

    pass
