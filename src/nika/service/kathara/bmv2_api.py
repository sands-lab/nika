from typing import List

import json

from nika.service.kathara.base_api import KatharaBaseAPI, _SupportsBase


def _build_thrift_command(api_calls: list[str]) -> str:
    """
    Build a bash command to execute multiple Thrift API calls on the switch.
    Each item in `api_calls` should be a valid method call string,
    e.g., "get_tables()", "get_registers('my_reg')"

    Note: use double quotes for the method calls to avoid escaping issues!
    """
    python_lines = [
        "from sswitch_thrift_API import SimpleSwitchThriftAPI",
        "simple_switch = SimpleSwitchThriftAPI(thrift_port=9090)",
    ]

    for call in api_calls:
        python_lines.append(f"print(simple_switch.{call})")

    python_script = "\n".join(python_lines)

    command = f"bash -c 'cd /usr/local/lib/python3.11/site-packages && python3 << EOF\n{python_script}\nEOF'"
    return command


def _quote_list_double(items: List[str]) -> str:
    """Turn a list of strings into a double-quoted string representation."""
    return "[" + ", ".join(f'"{item}"' for item in items) + "]"


class BMv2APIMixin:
    """
    Interfaces to interact with the Kathara BMv2 switches.
    """

    # Log related API
    def bmv2_get_log(
        self: _SupportsBase, switch_name: str, rows: int = 100
    ) -> list[str]:
        """
        Get the log file of a switch.
        """
        command = f"tail -n {rows} sw.log"
        return self.exec_cmd(switch_name, command)

    # Switch related API
    def bmv2_switch_info(self: _SupportsBase, switch_name: str) -> list[str]:
        """
        Show the switch info.
        """
        command = _build_thrift_command(["show_switch_info()"])
        return self.exec_cmd(switch_name, command)

    def bmv2_show_ports(self: _SupportsBase, switch_name: str) -> list[str]:
        """
        Show the ports of a switch.
        """
        command = _build_thrift_command(["show_ports()"])
        return self.exec_cmd(switch_name, command)

    def bmv2_show_tables(self: _SupportsBase, switch_name: str) -> list[str]:
        """
        Show the tables of a switch.
        """
        command = _build_thrift_command(["show_tables()"])
        return self.exec_cmd(switch_name, command)

    def bmv2_show_actions(self: _SupportsBase, switch_name: str) -> list[str]:
        """
        Show all actions of a switch.
        """
        command = _build_thrift_command(["show_actions()"])
        return self.exec_cmd(switch_name, command)

    def bmv2_get_register_arrays(self: _SupportsBase, switch_name: str) -> list[str]:
        """
        Show all register_arrays of a switch.
        """
        command = _build_thrift_command(["get_register_arrays()"])
        return self.exec_cmd(switch_name, command)

    def bmv2_register_read(
        self: _SupportsBase,
        switch_name: str,
        register_name: str,
        index: int = 0,
    ) -> list[str]:
        """
        Read a register.
        """
        command = _build_thrift_command([f'register_read("{register_name}", {index})'])
        return self.exec_cmd(switch_name, command)

    # Table related API
    def bmv2_table_info(
        self: _SupportsBase, switch_name: str, table_name: str
    ) -> list[str]:
        """
        Show the info of a table.
        """
        command = _build_thrift_command([f'table_info("{table_name}")'])
        return self.exec_cmd(switch_name, command)

    def bmv2_table_dump(
        self: _SupportsBase, switch_name: str, table_name: str
    ) -> list[str]:
        """
        Dump the content of a table.
        """
        command = _build_thrift_command([f'table_dump("{table_name}")'])
        return self.exec_cmd(switch_name, command)

    def bmv2_table_show_actions(
        self: _SupportsBase, switch_name: str, table_name: str
    ) -> list[str]:
        """
        Show the actions of a table.
        """
        command = _build_thrift_command([f'table_show_actions("{table_name}")'])
        return self.exec_cmd(switch_name, command)

    def bmv2_table_num_entries(
        self: _SupportsBase, switch_name: str, table_name: str
    ) -> list[str]:
        """
        Show the number of entries in a table.
        """
        command = _build_thrift_command([f'table_num_entries("{table_name}")'])
        return self.exec_cmd(switch_name, command)

    def bmv2_table_clear(
        self: _SupportsBase, switch_name: str, table_name: str
    ) -> list[str]:
        """
        Clear the content of a table.
        """
        command = _build_thrift_command([f'table_clear("{table_name}")'])
        return self.exec_cmd(switch_name, command)

    def bmv2_table_add(
        self: _SupportsBase,
        switch_name: str,
        table_name: str,
        action_name: str,
        match_keys: List[str],
        action_params: List[str] = [],
        prio: int = 0,
    ) -> list[str]:
        """
        Add an entry to a table.
        """
        match_keys_str = _quote_list_double(match_keys)
        action_params_str = _quote_list_double(action_params)

        command = _build_thrift_command(
            [
                f'table_add("{table_name}", "{action_name}", {match_keys_str}, {action_params_str}, {prio})'
            ]
        )
        return self.exec_cmd(switch_name, command)

    def bmv2_table_get_entry_handle(
        self: _SupportsBase,
        switch_name: str,
        table_name: str,
        match_keys: List[str],
    ) -> list[str]:
        """
        Get the entry handle of a table given the match keys.
        """
        match_keys_str = _quote_list_double(match_keys)
        command = _build_thrift_command(
            [f'get_handle_from_match("{table_name}", {match_keys_str})']
        )
        return self.exec_cmd(switch_name, command)

    def bmv2_table_set_timeout(
        self: _SupportsBase,
        switch_name: str,
        table_name: str,
        entry_handle: str,
        timeout_ms: int,
    ) -> list[str]:
        """
        Set the timeout of a table entry. The table has to support timeouts.
        """
        command = _build_thrift_command(
            [f'table_set_timeout("{table_name}", "{entry_handle}", {timeout_ms})']
        )
        return self.exec_cmd(switch_name, command)

    def bmv2_table_modify(
        self: _SupportsBase,
        switch_name: str,
        table_name: str,
        action_name: str,
        entry_handle: str,
        action_params: List[str] = [],
    ) -> list[str]:
        """
        Modify an entry in a table.
        """
        action_params_str = _quote_list_double(action_params)
        command = _build_thrift_command(
            [
                f'table_modify("{table_name}", "{action_name}", {entry_handle}, {action_params_str})'
            ]
        )
        return self.exec_cmd(switch_name, command)

    def bmv2_table_modify_match(
        self: _SupportsBase,
        switch_name: str,
        table_name: str,
        action_name: str,
        match_keys: List[str],
        action_params: List[str] = [],
    ) -> list[str]:
        """
        Modify entry in a table using match keys.
        """
        match_keys_str = _quote_list_double(match_keys)
        action_params_str = _quote_list_double(action_params)
        command = _build_thrift_command(
            [
                f'table_modify_match("{table_name}", "{action_name}", {match_keys_str}, {action_params_str})'
            ]
        )
        return self.exec_cmd(switch_name, command)

    def bmv2_table_delete(
        self: _SupportsBase,
        switch_name: str,
        table_name: str,
        entry_handle: str,
    ) -> list[str]:
        """
        Delete an entry from a table.
        """
        command = _build_thrift_command(
            [f'table_delete("{table_name}", "{entry_handle}")']
        )
        return self.exec_cmd(switch_name, command)

    def bmv2_table_delete_match(
        self: _SupportsBase,
        switch_name: str,
        table_name: str,
        match_keys: List[str],
    ) -> list[str]:
        """
        Delete an entry from a table using match keys.
        """
        match_keys_str = _quote_list_double(match_keys)
        command = _build_thrift_command(
            [f'table_delete_match("{table_name}", {match_keys_str})']
        )
        return self.exec_cmd(switch_name, command)

    # Counter related API
    def bmv2_get_counter_arrays(self: _SupportsBase, switch_name: str) -> list[str]:
        """
        Show all counter_arrays of a switch.
        """
        command = _build_thrift_command(["get_counter_arrays()"])
        return self.exec_cmd(switch_name, command)

    def bmv2_counter_read(
        self: _SupportsBase,
        switch_name: str,
        counter_name: str,
        index: int = 0,
    ) -> list[str]:
        """
        Read a counter.
        """
        command = _build_thrift_command([f'counter_read("{counter_name}", {index})'])
        return self.exec_cmd(switch_name, command)

    def read_p4_program(
        self: _SupportsBase,
        switch_name: str,
    ) -> list[str]:
        """
        Read the P4 program from the switch.
        """
        list_root = self.exec_cmd(switch_name, "ls *.p4 2>/dev/null || true").strip()
        if list_root:
            p4_files = list_root.split()
            if len(p4_files) > 0:
                f = p4_files[0]
                content = self.exec_cmd(switch_name, f"cat {f}")
                return content
            else:
                return ""

        # If not found, try p4_src/
        list_p4src = self.exec_cmd(
            switch_name, "ls p4_src/*.p4 2>/dev/null || true"
        ).strip()
        if list_p4src:
            p4_files = list_p4src.split()
            if len(p4_files) > 0:
                f = p4_files[0]
                content = self.exec_cmd(switch_name, f"cat {f}")
                return content
            else:
                return ""

        # Nothing found
        return ""

    def p4_get_fabric_state(
        self: _SupportsBase,
        switch_name: str | None = None,
        source: str | None = None,
        target_ip: str | None = None,
    ) -> dict:
        """Aggregated P4 fabric evidence. Does not name faults."""
        focus = switch_name or "leaf_1"
        summary_cmd = (
            "python3 - <<'PY'\n"
            "import json\n"
            "d=json.load(open('/tmp/p4_fabric/intent.json'))\n"
            f"n={focus!r}\n"
            "s=d['switches'][n]\n"
            "print(json.dumps({\n"
            " 'pipeline': d.get('pipeline'),\n"
            " 'spines': d.get('spines'),\n"
            " 'leaves': d.get('leaves'),\n"
            " 'endpoints': [{'name':e.get('name'),'ip':e.get('ip'),'role':e.get('role'),"
            "'leaf_id':e.get('leaf_id')} for e in d.get('endpoints') or []],\n"
            " 'switch': {n: {'role': s.get('role'), 'device_id': s.get('device_id'),\n"
            "  'address': s.get('address'),\n"
            "  'ipv4_lpm': s.get('ipv4_lpm'), 'groups': s.get('groups'),\n"
            "  'member_count': len(s.get('members') or [])}}\n"
            "}))\n"
            "PY"
        )
        intent_raw = self.exec_cmd("fabric_mgr", summary_cmd, timeout=20)
        try:
            intent = json.loads(intent_raw[intent_raw.find("{") :])
        except (json.JSONDecodeError, ValueError):
            intent = {}
        cmd = (
            "python3 /opt/nika/p4rt_manager.py --intent /tmp/p4_fabric/intent.json "
            "--p4info /tmp/p4_fabric/fabric.p4info.txt --json /tmp/p4_fabric/fabric.json "
            f"read --switch {focus}"
        )
        observed_raw = self.exec_cmd("fabric_mgr", cmd, timeout=90)
        try:
            start = observed_raw.find("{")
            observed = json.loads(observed_raw[start:]) if start >= 0 else {}
        except json.JSONDecodeError:
            observed = {"ok": False, "raw": observed_raw[-2000:]}
        sw = (intent.get("switch") or {}).get(focus) or {}
        payload = {
            "intended_forwarding": intent,
            "p4runtime_observed": observed.get("switches") or observed,
            "switch_inventory": [
                {
                    "name": focus,
                    "role": sw.get("role"),
                    "device_id": sw.get("device_id"),
                    "address": sw.get("address"),
                }
            ],
            "endpoint_addressing": intent.get("endpoints"),
        }
        if source and target_ip:
            ping_output = self.exec_cmd(
                source, f"ping -c 3 -W 2 {target_ip}", timeout=20
            )
            payload["traffic_observed"] = {
                "source": source,
                "target_ip": target_ip,
                "ping_output": ping_output,
            }
        return payload


class KatharaBMv2API(KatharaBaseAPI, BMv2APIMixin):
    """
    Kathara API for interacting with BMv2 switches.
    """

    pass
