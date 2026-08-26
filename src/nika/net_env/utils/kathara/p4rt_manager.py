#!/usr/bin/env python3
"""P4Runtime fabric manager (runs inside fabric_mgr)."""

from __future__ import annotations

import argparse
import ipaddress
import json
import queue
import sys
import threading
from typing import Any, Iterable

import grpc
from google.protobuf import text_format
from p4.config.v1 import p4info_pb2
from p4.v1 import p4runtime_pb2, p4runtime_pb2_grpc

TABLE_IPV4 = "ipv4_lpm"
ACTION_FWD = "ipv4_forward"
PROFILE = "ecmp_selector"
COUNTER_INGRESS = "ingress_port_counter"
COUNTER_EGRESS = "egress_port_counter"
ELECTION_LOW = 1


def _die(msg: str, code: int = 1) -> None:
    print(json.dumps({"ok": False, "error": msg}), file=sys.stderr)
    raise SystemExit(code)


def load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def load_p4info(path: str) -> p4info_pb2.P4Info:
    info = p4info_pb2.P4Info()
    with open(path, encoding="utf-8") as handle:
        text_format.Parse(handle.read(), info)
    return info


def _short(name: str) -> str:
    return name.rsplit(".", 1)[-1]


class P4InfoIndex:
    def __init__(self, p4info: p4info_pb2.P4Info):
        self.p4info = p4info
        self.tables = {_short(t.preamble.name): t for t in p4info.tables}
        self.actions = {_short(a.preamble.name): a for a in p4info.actions}
        self.profiles = {_short(p.preamble.name): p for p in p4info.action_profiles}
        self.counters = {_short(c.preamble.name): c for c in p4info.counters}
        self.registers = {_short(r.preamble.name): r for r in p4info.registers}

    def table_id(self, name: str) -> int:
        return int(self.tables[name].preamble.id)

    def action_id(self, name: str) -> int:
        return int(self.actions[name].preamble.id)

    def profile_id(self, name: str) -> int:
        return int(self.profiles[name].preamble.id)

    def counter_id(self, name: str) -> int:
        return int(self.counters[name].preamble.id)

    def register_id(self, name: str) -> int:
        return int(self.registers[name].preamble.id)

    def match_field_id(self, table: str, field: str) -> int:
        want = _short(field)
        for item in self.tables[table].match_fields:
            if _short(item.name) == want or item.name.endswith(field):
                return int(item.id)
        raise KeyError(f"match field {field} not in {table}")

    def match_field_bitwidth(self, table: str, field: str) -> int:
        want = _short(field)
        for item in self.tables[table].match_fields:
            if _short(item.name) == want or item.name.endswith(field):
                return int(item.bitwidth)
        raise KeyError(f"match field {field} not in {table}")

    def action_param_id(self, action: str, param: str) -> int:
        for item in self.actions[action].params:
            if item.name == param:
                return int(item.id)
        raise KeyError(f"param {param} not in {action}")

    def action_param_bitwidth(self, action: str, param: str) -> int:
        for item in self.actions[action].params:
            if item.name == param:
                return int(item.bitwidth)
        raise KeyError(f"param {param} not in {action}")

    def pipeline_name(self) -> str:
        return self.p4info.pkg_info.name or "unknown"


def encode_mac(mac: str) -> bytes:
    return bytes(int(part, 16) for part in mac.split(":"))


def encode_ipv4(addr: str) -> bytes:
    return ipaddress.IPv4Address(addr).packed


def encode_u(value: int, bitwidth: int) -> bytes:
    nbytes = (bitwidth + 7) // 8
    return int(value).to_bytes(nbytes, "big")


def decode_mac(raw: bytes) -> str:
    raw = raw[-6:]
    return ":".join(f"{b:02x}" for b in raw)


def decode_u(raw: bytes) -> int:
    return int.from_bytes(raw, "big") if raw else 0


def decode_prefix(value: bytes, prefix_len: int) -> str:
    padded = value.ljust(4, b"\x00")[:4]
    return f"{ipaddress.IPv4Address(padded)}/{prefix_len}"


class SwitchClient:
    def __init__(self, address: str, device_id: int):
        self.address = address
        self.device_id = device_id
        self.channel = grpc.insecure_channel(address)
        self.stub = p4runtime_pb2_grpc.P4RuntimeStub(self.channel)
        self._requests: queue.Queue = queue.Queue()
        self._arbitration = threading.Event()
        self._arbitration_error: str | None = None
        self.stream = self.stub.StreamChannel(self._iter_requests())
        self._listener = threading.Thread(target=self._listen, daemon=True)
        self._listener.start()
        self._become_master()

    def _iter_requests(self):
        while True:
            item = self._requests.get()
            if item is None:
                return
            yield item

    def _listen(self) -> None:
        try:
            for msg in self.stream:
                if msg.HasField("arbitration"):
                    status = msg.arbitration.status
                    if status.code != 0:
                        self._arbitration_error = (
                            f"arbitration lost: code={status.code} {status.message}"
                        )
                    self._arbitration.set()
        except grpc.RpcError as exc:
            self._arbitration_error = str(exc)
            self._arbitration.set()

    def _become_master(self) -> None:
        req = p4runtime_pb2.StreamMessageRequest()
        req.arbitration.device_id = self.device_id
        req.arbitration.election_id.high = 0
        req.arbitration.election_id.low = ELECTION_LOW
        self._requests.put(req)
        if not self._arbitration.wait(timeout=10):
            raise TimeoutError(f"P4Runtime arbitration timeout on {self.address}")
        if self._arbitration_error:
            raise RuntimeError(self._arbitration_error)

    def set_pipeline(self, p4info: p4info_pb2.P4Info, device_config: bytes) -> None:
        req = p4runtime_pb2.SetForwardingPipelineConfigRequest()
        req.device_id = self.device_id
        req.election_id.high = 0
        req.election_id.low = ELECTION_LOW
        req.action = p4runtime_pb2.SetForwardingPipelineConfigRequest.VERIFY_AND_COMMIT
        req.config.p4info.CopyFrom(p4info)
        req.config.p4_device_config = device_config
        req.config.cookie.cookie = 1
        self.stub.SetForwardingPipelineConfig(req, timeout=30)

    def get_pipeline(self) -> dict[str, Any]:
        req = p4runtime_pb2.GetForwardingPipelineConfigRequest()
        req.device_id = self.device_id
        req.response_type = (
            p4runtime_pb2.GetForwardingPipelineConfigRequest.P4INFO_AND_COOKIE
        )
        try:
            resp = self.stub.GetForwardingPipelineConfig(req, timeout=10)
        except grpc.RpcError as exc:
            return {"ok": False, "error": str(exc)}
        name = resp.config.p4info.pkg_info.name or ""
        cookie = int(resp.config.cookie.cookie)
        return {
            "ok": True,
            "pipeline_name": name,
            "cookie": cookie,
            "table_count": len(resp.config.p4info.tables),
        }

    def write(self, updates: list[p4runtime_pb2.Update], atomic: int) -> None:
        req = p4runtime_pb2.WriteRequest()
        req.device_id = self.device_id
        req.election_id.high = 0
        req.election_id.low = ELECTION_LOW
        req.atomicity = atomic
        req.updates.extend(updates)
        self.stub.Write(req, timeout=30)

    def write_continue(self, updates: list[p4runtime_pb2.Update]) -> str | None:
        try:
            self.write(
                updates,
                p4runtime_pb2.WriteRequest.CONTINUE_ON_ERROR,
            )
            return None
        except grpc.RpcError as exc:
            return str(exc)

    def read_entities(self, *builders: Iterable[p4runtime_pb2.Entity]) -> list:
        req = p4runtime_pb2.ReadRequest()
        req.device_id = self.device_id
        for entity in builders:
            req.entities.append(entity)
        entities = []
        try:
            for resp in self.stub.Read(req, timeout=30):
                entities.extend(resp.entities)
        except grpc.RpcError as exc:
            if (
                "Invalid P4 id" in str(exc)
                or exc.code() == grpc.StatusCode.UNIMPLEMENTED
            ):
                return []
            raise
        return entities

    def close(self) -> None:
        self._requests.put(None)
        self.channel.close()


def _fwd_action(index: P4InfoIndex, src_mac: str, dst_mac: str, port: int):
    action = p4runtime_pb2.Action()
    action.action_id = index.action_id(ACTION_FWD)
    for name, raw in (
        ("srcAddr", encode_mac(src_mac)),
        ("dstAddr", encode_mac(dst_mac)),
        ("port", encode_u(port, 9)),
    ):
        param = action.params.add()
        param.param_id = index.action_param_id(ACTION_FWD, name)
        param.value = raw
    return action


def member_entity(index: P4InfoIndex, member: dict[str, Any], *, insert: bool):
    entity = p4runtime_pb2.Entity()
    item = entity.action_profile_member
    item.action_profile_id = index.profile_id(PROFILE)
    item.member_id = int(member["member_id"])
    item.action.CopyFrom(
        _fwd_action(index, member["src_mac"], member["dst_mac"], int(member["port"]))
    )
    update = p4runtime_pb2.Update()
    update.type = p4runtime_pb2.Update.INSERT if insert else p4runtime_pb2.Update.MODIFY
    update.entity.CopyFrom(entity)
    return update


def group_entity(index: P4InfoIndex, group: dict[str, Any], *, insert: bool):
    entity = p4runtime_pb2.Entity()
    item = entity.action_profile_group
    item.action_profile_id = index.profile_id(PROFILE)
    item.group_id = int(group["group_id"])
    for member_id in group["member_ids"]:
        mem = item.members.add()
        mem.member_id = int(member_id)
        mem.weight = 1
    update = p4runtime_pb2.Update()
    update.type = p4runtime_pb2.Update.INSERT if insert else p4runtime_pb2.Update.MODIFY
    update.entity.CopyFrom(entity)
    return update


def lpm_entity(index: P4InfoIndex, entry: dict[str, Any], *, insert: bool):
    prefix = entry["prefix"]
    network = ipaddress.IPv4Network(prefix, strict=False)
    entity = p4runtime_pb2.Entity()
    table = entity.table_entry
    table.table_id = index.table_id(TABLE_IPV4)
    mf = table.match.add()
    mf.field_id = index.match_field_id(TABLE_IPV4, "dstAddr")
    mf.lpm.value = encode_ipv4(str(network.network_address))
    mf.lpm.prefix_len = int(network.prefixlen)
    table.action.action_profile_group_id = int(entry["group_id"])
    update = p4runtime_pb2.Update()
    update.type = p4runtime_pb2.Update.INSERT if insert else p4runtime_pb2.Update.DELETE
    update.entity.CopyFrom(entity)
    return update


def direct_lpm_entity(
    index: P4InfoIndex, table_name: str, action_name: str, prefix: str
):
    network = ipaddress.IPv4Network(prefix, strict=False)
    entity = p4runtime_pb2.Entity()
    table = entity.table_entry
    table.table_id = index.table_id(table_name)
    mf = table.match.add()
    mf.field_id = index.match_field_id(table_name, "dstAddr")
    mf.lpm.value = encode_ipv4(str(network.network_address))
    mf.lpm.prefix_len = int(network.prefixlen)
    table.action.action.action_id = index.action_id(action_name)
    update = p4runtime_pb2.Update()
    update.type = p4runtime_pb2.Update.INSERT
    update.entity.CopyFrom(entity)
    return update


def default_action_entity(
    index: P4InfoIndex, table_name: str, action_name: str, params: dict[str, int]
):
    entity = p4runtime_pb2.Entity()
    entry = entity.table_entry
    entry.table_id = index.table_id(table_name)
    entry.is_default_action = True
    entry.action.action.action_id = index.action_id(action_name)
    for name, value in params.items():
        param = entry.action.action.params.add()
        param.param_id = index.action_param_id(action_name, name)
        param.value = encode_u(value, index.action_param_bitwidth(action_name, name))
    update = p4runtime_pb2.Update()
    update.type = p4runtime_pb2.Update.MODIFY
    update.entity.CopyFrom(entity)
    return update


def exact_table_entity(
    index: P4InfoIndex,
    table_name: str,
    field_name: str,
    key: int,
    action_name: str,
    params: dict[str, int],
    *,
    modify: bool = False,
):
    entity = p4runtime_pb2.Entity()
    entry = entity.table_entry
    entry.table_id = index.table_id(table_name)
    match = entry.match.add()
    match.field_id = index.match_field_id(table_name, field_name)
    match.exact.value = encode_u(
        key, index.match_field_bitwidth(table_name, field_name)
    )
    entry.action.action.action_id = index.action_id(action_name)
    for name, value in params.items():
        param = entry.action.action.params.add()
        param.param_id = index.action_param_id(action_name, name)
        param.value = encode_u(value, index.action_param_bitwidth(action_name, name))
    update = p4runtime_pb2.Update()
    update.type = p4runtime_pb2.Update.MODIFY if modify else p4runtime_pb2.Update.INSERT
    update.entity.CopyFrom(entity)
    return update


def exact_ipv4_table_entity(
    index: P4InfoIndex, table_name: str, action_name: str, address: str
):
    entity = p4runtime_pb2.Entity()
    entry = entity.table_entry
    entry.table_id = index.table_id(table_name)
    match = entry.match.add()
    match.field_id = index.match_field_id(table_name, "dstAddr")
    match.exact.value = encode_ipv4(address)
    entry.action.action.action_id = index.action_id(action_name)
    update = p4runtime_pb2.Update()
    update.type = p4runtime_pb2.Update.INSERT
    update.entity.CopyFrom(entity)
    return update


def exact_fields_entity(
    index: P4InfoIndex,
    table_name: str,
    fields: dict[str, int | str],
    action_name: str,
    params: dict[str, int | str],
    *,
    modify: bool = False,
):
    """Build an exact-match entry for small gateway runtime tables."""
    entity = p4runtime_pb2.Entity()
    entry = entity.table_entry
    entry.table_id = index.table_id(table_name)
    for field, value in fields.items():
        match = entry.match.add()
        match.field_id = index.match_field_id(table_name, field)
        width = index.match_field_bitwidth(table_name, field)
        match.exact.value = (
            encode_ipv4(value) if isinstance(value, str) else encode_u(value, width)
        )
    entry.action.action.action_id = index.action_id(action_name)
    for name, value in params.items():
        param = entry.action.action.params.add()
        param.param_id = index.action_param_id(action_name, name)
        width = index.action_param_bitwidth(action_name, name)
        param.value = (
            encode_ipv4(value) if isinstance(value, str) else encode_u(value, width)
        )
    update = p4runtime_pb2.Update()
    update.type = p4runtime_pb2.Update.MODIFY if modify else p4runtime_pb2.Update.INSERT
    update.entity.CopyFrom(entity)
    return update


def connect(switch: dict[str, Any]) -> SwitchClient:
    return SwitchClient(switch["address"], int(switch["device_id"]))


def apply_switch(
    client: SwitchClient,
    index: P4InfoIndex,
    switch: dict[str, Any],
    p4info: p4info_pb2.P4Info,
    device_config: bytes,
) -> None:
    client.set_pipeline(p4info, device_config)
    updates = [member_entity(index, m, insert=True) for m in switch["members"]]
    updates.extend(group_entity(index, g, insert=True) for g in switch["groups"])
    updates.extend(lpm_entity(index, e, insert=True) for e in switch["ipv4_lpm"])
    client.write(updates, p4runtime_pb2.WriteRequest.CONTINUE_ON_ERROR)
    if "role_id" in switch:
        runtime_updates = [
            (
                "runtime_role",
                default_action_entity(
                    index, "runtime_role", "set_role", {"role": switch["role_id"]}
                ),
            )
        ]
        for raw_port, threshold in switch.get("ecn", {}).items():
            port = int(raw_port)
            runtime_updates.extend(
                (
                    (
                        f"ecn_config[{port}]",
                        exact_table_entity(
                            index,
                            "ecn_config",
                            "egress_port",
                            port,
                            "set_ecn_threshold",
                            {"threshold": int(threshold)},
                        ),
                    ),
                    (
                        f"int_mtu_config[{port}]",
                        exact_table_entity(
                            index,
                            "int_mtu_config",
                            "egress_spec",
                            port,
                            "set_int_mtu",
                            {"mtu": int(switch["int"]["mtu"])},
                        ),
                    ),
                )
            )
        if switch.get("role") == "gateway":
            runtime_updates.append(
                (
                    "int_watchlist",
                    direct_lpm_entity(
                        index, "int_watchlist", "watch_int", "10.0.0.0/8"
                    ),
                )
            )
            lb = switch.get("l4_load_balancer") or {}
            if lb:
                vip = lb["vip"]
                runtime_updates.append(
                    (
                        "lb_vip",
                        exact_fields_entity(
                            index,
                            "lb_vip",
                            {
                                "dstAddr": vip["ip"],
                                "dstPort": int(vip["port"]),
                                "protocol": 6,
                            },
                            "set_lb_vip",
                            {"version": int(lb["pool_version"])},
                        ),
                    )
                )
                backends = lb["backends"]
                for bucket in range(int(lb["hash"]["buckets"])):
                    runtime_updates.append(
                        (
                            f"lb_pool[{bucket}]",
                            exact_fields_entity(
                                index,
                                "lb_pool",
                                {
                                    "lbPoolVersion": int(lb["pool_version"]),
                                    "lbBucket": bucket,
                                },
                                "set_pool_dip",
                                {"dip": backends[bucket % len(backends)]["dip"]},
                            ),
                        )
                    )
        for label, update in runtime_updates:
            try:
                client.write([update], p4runtime_pb2.WriteRequest.CONTINUE_ON_ERROR)
            except grpc.RpcError as exc:
                raise RuntimeError(f"failed to write {label}: {exc}") from exc


def observed_switch(client: SwitchClient, index: P4InfoIndex) -> dict[str, Any]:
    pipeline = client.get_pipeline()
    members = []
    groups = []
    ipv4_lpm = []
    counters = {"ingress": {}, "egress": {}, "flow_syn": {}, "flow_non_syn": {}}
    registers: dict[str, Any] = {}
    runtime_config: dict[str, list[dict[str, int]]] = {}

    wildcard = p4runtime_pb2.Entity()
    wildcard.action_profile_member.action_profile_id = index.profile_id(PROFILE)
    group_w = p4runtime_pb2.Entity()
    group_w.action_profile_group.action_profile_id = index.profile_id(PROFILE)
    table_w = p4runtime_pb2.Entity()
    table_w.table_entry.table_id = index.table_id(TABLE_IPV4)
    for entity in client.read_entities(wildcard, group_w, table_w):
        if entity.HasField("action_profile_member"):
            item = entity.action_profile_member
            params = {p.param_id: p.value for p in item.action.params}
            src_id = index.action_param_id(ACTION_FWD, "srcAddr")
            dst_id = index.action_param_id(ACTION_FWD, "dstAddr")
            port_id = index.action_param_id(ACTION_FWD, "port")
            members.append(
                {
                    "member_id": int(item.member_id),
                    "src_mac": decode_mac(params.get(src_id, b"")),
                    "dst_mac": decode_mac(params.get(dst_id, b"")),
                    "port": decode_u(params.get(port_id, b"")),
                }
            )
        elif entity.HasField("action_profile_group"):
            item = entity.action_profile_group
            groups.append(
                {
                    "group_id": int(item.group_id),
                    "member_ids": [int(m.member_id) for m in item.members],
                }
            )
        elif entity.HasField("table_entry"):
            item = entity.table_entry
            prefix = None
            for mf in item.match:
                if mf.HasField("lpm"):
                    prefix = decode_prefix(mf.lpm.value, mf.lpm.prefix_len)
            ipv4_lpm.append(
                {
                    "prefix": prefix,
                    "group_id": int(item.action.action_profile_group_id),
                }
            )

    for name, bucket in (
        (COUNTER_INGRESS, "ingress"),
        (COUNTER_EGRESS, "egress"),
        ("flow_syn_total", "flow_syn"),
        ("flow_non_syn_total", "flow_non_syn"),
    ):
        if name not in index.counters:
            continue
        cent = p4runtime_pb2.Entity()
        cent.counter_entry.counter_id = index.counter_id(name)
        for entity in client.read_entities(cent):
            if entity.HasField("counter_entry"):
                entry = entity.counter_entry
                packets = int(entry.data.packet_count)
                bytes_ = int(entry.data.byte_count)
                if packets or bytes_:
                    counters[bucket][int(entry.index.index)] = {
                        "packets": packets,
                        "bytes": bytes_,
                    }

    for name in ("queue_occupancy",):
        if name not in index.registers:
            continue
        request = p4runtime_pb2.Entity()
        request.register_entry.register_id = index.register_id(name)
        values = {}
        for entity in client.read_entities(request):
            if entity.HasField("register_entry"):
                entry = entity.register_entry
                values[int(entry.index.index)] = decode_u(entry.data.bitstring)
        registers[name] = values

    for table_name in ("ecn_config", "int_mtu_config", "runtime_role"):
        if table_name not in index.tables:
            continue
        request = p4runtime_pb2.Entity()
        request.table_entry.table_id = index.table_id(table_name)
        values = []
        for entity in client.read_entities(request):
            if not entity.HasField("table_entry"):
                continue
            entry = entity.table_entry
            row = {
                "key": decode_u(entry.match[0].exact.value) if entry.match else 0,
                "is_default": int(entry.is_default_action),
            }
            if entry.action.HasField("action"):
                for param in entry.action.action.params:
                    row[f"param_{param.param_id}"] = decode_u(param.value)
            values.append(row)
        runtime_config[table_name] = values

    return {
        "pipeline": pipeline,
        "members": sorted(members, key=lambda m: m["member_id"]),
        "groups": sorted(groups, key=lambda g: g["group_id"]),
        "ipv4_lpm": sorted(ipv4_lpm, key=lambda e: e["prefix"] or ""),
        "counters": counters,
        "registers": registers,
        "runtime_config": runtime_config,
        "arbitration": {"master": True, "election_id": ELECTION_LOW},
    }


def intent_vs_observed(switch: dict[str, Any], observed: dict[str, Any]) -> list[str]:
    mismatches: list[str] = []
    want_groups = {
        int(g["group_id"]): sorted(int(m) for m in g["member_ids"])
        for g in switch["groups"]
    }
    got_groups = {
        int(g["group_id"]): sorted(int(m) for m in g["member_ids"])
        for g in observed.get("groups", [])
    }
    if want_groups != got_groups:
        mismatches.append("groups")
    want_lpm = {e["prefix"]: int(e["group_id"]) for e in switch["ipv4_lpm"]}
    got_lpm = {
        e["prefix"]: int(e["group_id"])
        for e in observed.get("ipv4_lpm", [])
        if e.get("prefix")
    }
    if want_lpm != got_lpm:
        mismatches.append("ipv4_lpm")
    want_members = {
        int(m["member_id"]): (
            m["src_mac"].lower(),
            m["dst_mac"].lower(),
            int(m["port"]),
        )
        for m in switch["members"]
    }
    got_members = {
        int(m["member_id"]): (
            str(m.get("src_mac", "")).lower(),
            str(m.get("dst_mac", "")).lower(),
            int(m.get("port") or 0),
        )
        for m in observed.get("members", [])
    }
    if want_members != got_members:
        mismatches.append("members")
    return mismatches


def cmd_apply(args: argparse.Namespace) -> None:
    intent = load_json(args.intent)
    p4info = load_p4info(args.p4info)
    index = P4InfoIndex(p4info)
    with open(args.json, "rb") as handle:
        device_config = handle.read()
    names = [args.switch] if args.switch else list(intent["switches"])
    results = {}
    for name in names:
        switch = intent["switches"][name]
        client = connect(switch)
        try:
            apply_switch(client, index, switch, p4info, device_config)
            observed = observed_switch(client, index)
            mismatches = intent_vs_observed(switch, observed)
            results[name] = {
                "ok": not mismatches,
                "mismatches": mismatches,
                "pipeline": observed.get("pipeline"),
                "ipv4_lpm_count": len(observed.get("ipv4_lpm") or []),
                "member_count": len(observed.get("members") or []),
                "group_count": len(observed.get("groups") or []),
            }
        finally:
            client.close()
    ok = all(row["ok"] for row in results.values())
    print(json.dumps({"ok": ok, "switches": results}))
    if not ok:
        raise SystemExit(2)


def cmd_read(args: argparse.Namespace) -> None:
    intent = load_json(args.intent)
    p4info = load_p4info(args.p4info)
    index = P4InfoIndex(p4info)
    names = [args.switch] if args.switch else list(intent["switches"])
    payload = {}
    for name in names:
        switch = intent["switches"][name]
        client = connect(switch)
        try:
            observed = observed_switch(client, index)
            payload[name] = observed
            payload[name]["mismatches"] = intent_vs_observed(switch, observed)
        finally:
            client.close()
    print(json.dumps({"ok": True, "switches": payload}, default=str))


def cmd_delete_lpm(args: argparse.Namespace) -> None:
    intent = load_json(args.intent)
    p4info = load_p4info(args.p4info)
    index = P4InfoIndex(p4info)
    switch = intent["switches"][args.switch]
    entry = next(e for e in switch["ipv4_lpm"] if e["prefix"] == args.prefix)
    client = connect(switch)
    try:
        client.write(
            [lpm_entity(index, entry, insert=False)],
            p4runtime_pb2.WriteRequest.CONTINUE_ON_ERROR,
        )
        observed = observed_switch(client, index)
        present = any(e.get("prefix") == args.prefix for e in observed["ipv4_lpm"])
        print(
            json.dumps(
                {
                    "ok": not present,
                    "prefix": args.prefix,
                    "present": present,
                    "ipv4_lpm": observed["ipv4_lpm"],
                }
            )
        )
    finally:
        client.close()


def cmd_modify_lpm_group(args: argparse.Namespace) -> None:
    intent = load_json(args.intent)
    p4info = load_p4info(args.p4info)
    index = P4InfoIndex(p4info)
    switch = intent["switches"][args.switch]
    entry = dict(next(e for e in switch["ipv4_lpm"] if e["prefix"] == args.prefix))
    client = connect(switch)
    try:
        client.write(
            [lpm_entity(index, entry, insert=False)],
            p4runtime_pb2.WriteRequest.CONTINUE_ON_ERROR,
        )
        entry["group_id"] = int(args.group_id)
        client.write(
            [lpm_entity(index, entry, insert=True)],
            p4runtime_pb2.WriteRequest.CONTINUE_ON_ERROR,
        )
        observed = observed_switch(client, index)
        got = next(
            (e for e in observed["ipv4_lpm"] if e.get("prefix") == args.prefix),
            None,
        )
        print(
            json.dumps(
                {
                    "ok": got is not None
                    and int(got["group_id"]) == int(args.group_id),
                    "prefix": args.prefix,
                    "group_id": None if got is None else got["group_id"],
                }
            )
        )
    finally:
        client.close()


def cmd_modify_member(args: argparse.Namespace) -> None:
    intent = load_json(args.intent)
    p4info = load_p4info(args.p4info)
    index = P4InfoIndex(p4info)
    switch = intent["switches"][args.switch]
    member = dict(
        next(m for m in switch["members"] if int(m["member_id"]) == int(args.member_id))
    )
    if args.dst_mac:
        member["dst_mac"] = args.dst_mac
    if args.port is not None:
        member["port"] = int(args.port)
    client = connect(switch)
    try:
        client.write(
            [member_entity(index, member, insert=False)],
            p4runtime_pb2.WriteRequest.CONTINUE_ON_ERROR,
        )
        observed = observed_switch(client, index)
        got = next(
            m for m in observed["members"] if int(m["member_id"]) == int(args.member_id)
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "member": got,
                    "expected_port": int(member["port"]),
                    "expected_dst_mac": member["dst_mac"].lower(),
                }
            )
        )
    finally:
        client.close()


def cmd_delete_group_member(args: argparse.Namespace) -> None:
    intent = load_json(args.intent)
    p4info = load_p4info(args.p4info)
    index = P4InfoIndex(p4info)
    switch = intent["switches"][args.switch]
    group = dict(
        next(g for g in switch["groups"] if int(g["group_id"]) == int(args.group_id))
    )
    group["member_ids"] = [
        mid for mid in group["member_ids"] if int(mid) != int(args.member_id)
    ]
    client = connect(switch)
    try:
        client.write(
            [group_entity(index, group, insert=False)],
            p4runtime_pb2.WriteRequest.CONTINUE_ON_ERROR,
        )
        observed = observed_switch(client, index)
        got = next(
            g for g in observed["groups"] if int(g["group_id"]) == int(args.group_id)
        )
        print(
            json.dumps(
                {
                    "ok": int(args.member_id)
                    not in [int(m) for m in got["member_ids"]],
                    "group": got,
                }
            )
        )
    finally:
        client.close()


def cmd_set_pipeline(args: argparse.Namespace) -> None:
    intent = load_json(args.intent)
    p4info = load_p4info(args.p4info)
    with open(args.json, "rb") as handle:
        device_config = handle.read()
    switch = intent["switches"][args.switch]
    client = connect(switch)
    try:
        err = None
        try:
            client.set_pipeline(p4info, device_config)
        except grpc.RpcError as exc:
            err = str(exc)
        pipeline = client.get_pipeline()
        print(
            json.dumps(
                {
                    "ok": True,
                    "set_error": err,
                    "pipeline": pipeline,
                }
            )
        )
    finally:
        client.close()


def cmd_partial_write(args: argparse.Namespace) -> None:
    intent = load_json(args.intent)
    p4info = load_p4info(args.p4info)
    index = P4InfoIndex(p4info)
    switch = intent["switches"][args.switch]
    entry = next(e for e in switch["ipv4_lpm"] if e["prefix"] == args.prefix)
    good = lpm_entity(index, entry, insert=False)
    bad = p4runtime_pb2.Update()
    bad.type = p4runtime_pb2.Update.INSERT
    bad.entity.table_entry.table_id = 0x7FFFFFFF
    mf = bad.entity.table_entry.match.add()
    mf.field_id = 1
    mf.lpm.value = encode_ipv4("203.0.113.1")
    mf.lpm.prefix_len = 32
    client = connect(switch)
    try:
        err = client.write_continue([good, bad])
        observed = observed_switch(client, index)
        present = any(e.get("prefix") == args.prefix for e in observed["ipv4_lpm"])
        remaining = len(observed["ipv4_lpm"])
        print(
            json.dumps(
                {
                    "ok": (not present) and remaining > 0,
                    "prefix_present": present,
                    "remaining_lpm": remaining,
                    "write_error": err,
                }
            )
        )
    finally:
        client.close()


def cmd_fill_table(args: argparse.Namespace) -> None:
    intent = load_json(args.intent)
    p4info = load_p4info(args.p4info)
    index = P4InfoIndex(p4info)
    switch = intent["switches"][args.switch]
    size = int(args.size)
    group_id = int(switch["ipv4_lpm"][0]["group_id"])
    client = connect(switch)
    added = 0
    failed = None
    try:
        observed = observed_switch(client, index)
        existing = {e["prefix"] for e in observed["ipv4_lpm"] if e.get("prefix")}
        occupancy = len(existing)
        if occupancy < size:
            for net in range(0, 256):
                stop = False
                for host in range(0, 256):
                    if occupancy + added >= size:
                        stop = True
                        break
                    prefix = f"198.{net}.{host}.1/32"
                    if prefix in existing:
                        continue
                    entry = {"prefix": prefix, "group_id": group_id}
                    try:
                        client.write(
                            [lpm_entity(index, entry, insert=True)],
                            p4runtime_pb2.WriteRequest.CONTINUE_ON_ERROR,
                        )
                        added += 1
                        existing.add(prefix)
                    except grpc.RpcError as exc:
                        failed = str(exc)
                        stop = True
                        break
                if stop or failed is not None:
                    break
            observed = observed_switch(client, index)
            occupancy = len(observed["ipv4_lpm"])
        print(
            json.dumps(
                {
                    "ok": occupancy >= size,
                    "occupancy": occupancy,
                    "size": size,
                    "added": added,
                    "write_error": failed,
                }
            )
        )
    finally:
        client.close()


def cmd_counters(args: argparse.Namespace) -> None:
    intent = load_json(args.intent)
    p4info = load_p4info(args.p4info)
    index = P4InfoIndex(p4info)
    names = [args.switch] if args.switch else list(intent["switches"])
    payload = {}
    for name in names:
        client = connect(intent["switches"][name])
        try:
            observed = observed_switch(client, index)
            payload[name] = observed["counters"]
        finally:
            client.close()
    print(json.dumps({"ok": True, "counters": payload}))


def cmd_gateway_config(args: argparse.Namespace) -> None:
    intent = load_json(args.intent)
    p4info = load_p4info(args.p4info)
    index = P4InfoIndex(p4info)
    client = connect(intent["switches"][args.switch])
    try:
        if args.kind == "silent-drop":
            update = exact_ipv4_table_entity(
                index, "internal_fault_drop", "fault_drop", args.address
            )
        elif args.kind == "icmp-frag-needed":
            update = exact_fields_entity(
                index,
                "icmp_frag_needed_acl",
                {"type": 3, "code": 4},
                "icmp_frag_needed_drop",
                {},
            )
        else:
            spec = {
                "loss": (
                    "internal_fault_loss_config",
                    "egress_spec",
                    "set_fault_loss_threshold",
                    "threshold",
                ),
                "ecn": (
                    "ecn_config",
                    "egress_port",
                    "set_ecn_threshold",
                    "threshold",
                ),
                "int-mtu": (
                    "int_mtu_config",
                    "egress_spec",
                    "set_int_mtu",
                    "mtu",
                ),
            }[args.kind]
            table, field, action, param = spec
            update = exact_table_entity(
                index,
                table,
                field,
                args.port,
                action,
                {param: args.value},
                modify=args.kind in {"ecn", "int-mtu"},
            )
        client.write([update], p4runtime_pb2.WriteRequest.CONTINUE_ON_ERROR)
        print(
            json.dumps(
                {
                    "ok": True,
                    "switch": args.switch,
                    "kind": args.kind,
                    "port": args.port,
                    "value": args.value,
                    "address": args.address,
                }
            )
        )
    finally:
        client.close()


def _gateway_lb_entries(client: SwitchClient, index: P4InfoIndex) -> dict[str, int]:
    """Count the live L4 tables; this is the fault evidence API."""
    names = ("lb_vip", "lb_conn_table", "lb_transit_table", "lb_pool")
    counts: dict[str, int] = {}
    for name in names:
        entity = p4runtime_pb2.Entity()
        entity.table_entry.table_id = index.table_id(name)
        counts[name] = sum(
            1
            for item in client.read_entities(entity)
            if item.HasField("table_entry")
            and item.table_entry.table_id == index.table_id(name)
        )
    return counts


def cmd_gateway_lb(args: argparse.Namespace) -> None:
    """Program the real gateway L4 tables used by the fault injectors."""
    intent = load_json(args.intent)
    p4info = load_p4info(args.p4info)
    index = P4InfoIndex(p4info)
    switch = intent["switches"][args.switch]
    lb = switch["l4_load_balancer"]
    vip = lb["vip"]
    backends = lb["backends"]
    client = connect(switch)
    try:
        if args.kind == "state":
            print(
                json.dumps(
                    {
                        "ok": True,
                        "switch": args.switch,
                        "tables": _gateway_lb_entries(client, index),
                    }
                )
            )
            return

        updates: list[p4runtime_pb2.Update] = []
        if args.kind == "exhaust":
            for offset in range(args.capacity):
                updates.append(
                    exact_fields_entity(
                        index,
                        "lb_conn_table",
                        {
                            "srcAddr": "192.0.2.10",
                            "dstAddr": vip["ip"],
                            "protocol": 6,
                            "srcPort": 20000 + offset,
                            "dstPort": int(vip["port"]),
                        },
                        "set_lb_dip",
                        {"dip": backends[0]["dip"]},
                    )
                )
        elif args.kind == "unsafe-update":
            updates.append(
                exact_fields_entity(
                    index,
                    "lb_vip",
                    {"dstAddr": vip["ip"], "dstPort": int(vip["port"]), "protocol": 6},
                    "set_lb_vip",
                    {"version": 2},
                    modify=True,
                )
            )
            for bucket in range(int(lb["hash"]["buckets"])):
                updates.append(
                    exact_fields_entity(
                        index,
                        "lb_pool",
                        {"lbPoolVersion": 2, "lbBucket": bucket},
                        "set_pool_dip",
                        {"dip": backends[1]["dip"]},
                    )
                )
        else:
            raise ValueError(f"unsupported gateway L4 operation: {args.kind}")

        client.write(updates, p4runtime_pb2.WriteRequest.CONTINUE_ON_ERROR)
        print(
            json.dumps(
                {
                    "ok": True,
                    "switch": args.switch,
                    "kind": args.kind,
                    "tables": _gateway_lb_entries(client, index),
                }
            )
        )
    finally:
        client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="P4Runtime fabric manager")
    parser.add_argument("--intent", default="/tmp/p4_fabric/intent.json")
    parser.add_argument("--p4info", default="/tmp/p4_fabric/fabric.p4info.txt")
    parser.add_argument("--json", default="/tmp/p4_fabric/fabric.json")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_apply = sub.add_parser("apply")
    p_apply.add_argument("--switch")
    p_apply.set_defaults(func=cmd_apply)

    p_read = sub.add_parser("read")
    p_read.add_argument("--switch")
    p_read.set_defaults(func=cmd_read)

    p_del = sub.add_parser("delete-lpm")
    p_del.add_argument("--switch", required=True)
    p_del.add_argument("--prefix", required=True)
    p_del.set_defaults(func=cmd_delete_lpm)

    p_modg = sub.add_parser("modify-lpm-group")
    p_modg.add_argument("--switch", required=True)
    p_modg.add_argument("--prefix", required=True)
    p_modg.add_argument("--group-id", required=True)
    p_modg.set_defaults(func=cmd_modify_lpm_group)

    p_mod = sub.add_parser("modify-member")
    p_mod.add_argument("--switch", required=True)
    p_mod.add_argument("--member-id", required=True)
    p_mod.add_argument("--dst-mac")
    p_mod.add_argument("--port", type=int)
    p_mod.set_defaults(func=cmd_modify_member)

    p_dg = sub.add_parser("delete-group-member")
    p_dg.add_argument("--switch", required=True)
    p_dg.add_argument("--group-id", required=True)
    p_dg.add_argument("--member-id", required=True)
    p_dg.set_defaults(func=cmd_delete_group_member)

    p_pipe = sub.add_parser("set-pipeline")
    p_pipe.add_argument("--switch", required=True)
    p_pipe.set_defaults(func=cmd_set_pipeline)

    p_part = sub.add_parser("partial-write")
    p_part.add_argument("--switch", required=True)
    p_part.add_argument("--prefix", required=True)
    p_part.set_defaults(func=cmd_partial_write)

    p_fill = sub.add_parser("fill-table")
    p_fill.add_argument("--switch", required=True)
    p_fill.add_argument("--size", type=int, default=256)
    p_fill.set_defaults(func=cmd_fill_table)

    p_ctr = sub.add_parser("counters")
    p_ctr.add_argument("--switch")
    p_ctr.set_defaults(func=cmd_counters)

    p_config = sub.add_parser("gateway-config")
    p_config.add_argument("--switch", required=True)
    p_config.add_argument(
        "--kind",
        required=True,
        choices=("silent-drop", "loss", "ecn", "int-mtu", "icmp-frag-needed"),
    )
    p_config.add_argument("--port", type=int)
    p_config.add_argument("--value", type=int)
    p_config.add_argument("--address")
    p_config.set_defaults(func=cmd_gateway_config)

    p_lb = sub.add_parser("gateway-lb")
    p_lb.add_argument("--switch", required=True)
    p_lb.add_argument(
        "--kind", required=True, choices=("state", "exhaust", "unsafe-update")
    )
    p_lb.add_argument("--capacity", type=int, default=256)
    p_lb.set_defaults(func=cmd_gateway_lb)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
