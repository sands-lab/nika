#!/usr/bin/env python3
"""P4Runtime fabric manager (runs inside fabric_mgr).

``apply`` pushes the pipeline and the forwarding intent to every switch and
records each Write it sends, with the per-update status, in ``LOG_PATH``.
``read``, ``read-table`` and ``counters`` query the live P4Runtime state.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import queue
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

import grpc
from google.protobuf import text_format
from google.rpc import code_pb2, status_pb2
from p4.config.v1 import p4info_pb2
from p4.v1 import p4runtime_pb2, p4runtime_pb2_grpc

TABLE_IPV4 = "ipv4_lpm"
ACTION_FWD = "ipv4_forward"
PROFILE = "ecmp_selector"
COUNTER_INGRESS = "ingress_port_counter"
COUNTER_EGRESS = "egress_port_counter"
ELECTION_LOW = 1
LOG_PATH = "/var/log/p4rt/controller.log"


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
            "tables": sorted(t.preamble.name for t in resp.config.p4info.tables),
        }

    def write(self, updates: list[p4runtime_pb2.Update], atomic: int) -> None:
        req = p4runtime_pb2.WriteRequest()
        req.device_id = self.device_id
        req.election_id.high = 0
        req.election_id.low = ELECTION_LOW
        req.atomicity = atomic
        req.updates.extend(updates)
        self.stub.Write(req, timeout=30)

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


_UPDATE_TYPES = {
    "INSERT": p4runtime_pb2.Update.INSERT,
    "MODIFY": p4runtime_pb2.Update.MODIFY,
    "DELETE": p4runtime_pb2.Update.DELETE,
}
_ATOMICITY = {
    "CONTINUE_ON_ERROR": p4runtime_pb2.WriteRequest.CONTINUE_ON_ERROR,
    "ROLLBACK_ON_ERROR": p4runtime_pb2.WriteRequest.ROLLBACK_ON_ERROR,
    "DATAPLANE_ATOMIC": p4runtime_pb2.WriteRequest.DATAPLANE_ATOMIC,
}


def _update(entity: p4runtime_pb2.Entity, kind: str) -> p4runtime_pb2.Update:
    update = p4runtime_pb2.Update()
    update.type = _UPDATE_TYPES[kind]
    update.entity.CopyFrom(entity)
    return update


def _encode_value(value: int | str, bitwidth: int) -> bytes:
    if isinstance(value, str) and "." in value:
        return encode_ipv4(value)
    if isinstance(value, str) and ":" in value:
        return encode_mac(value)
    return encode_u(int(value), bitwidth)


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


def member_entity(index: P4InfoIndex, member: dict[str, Any], *, kind: str = "INSERT"):
    entity = p4runtime_pb2.Entity()
    item = entity.action_profile_member
    item.action_profile_id = index.profile_id(PROFILE)
    item.member_id = int(member["member_id"])
    if kind != "DELETE":
        item.action.CopyFrom(
            _fwd_action(
                index, member["src_mac"], member["dst_mac"], int(member["port"])
            )
        )
    return _update(entity, kind)


def group_entity(index: P4InfoIndex, group: dict[str, Any], *, kind: str = "INSERT"):
    entity = p4runtime_pb2.Entity()
    item = entity.action_profile_group
    item.action_profile_id = index.profile_id(PROFILE)
    item.group_id = int(group["group_id"])
    if kind != "DELETE":
        for member_id in group["member_ids"]:
            mem = item.members.add()
            mem.member_id = int(member_id)
            mem.weight = 1
    return _update(entity, kind)


def lpm_entity(index: P4InfoIndex, entry: dict[str, Any], *, kind: str = "INSERT"):
    network = ipaddress.IPv4Network(entry["prefix"], strict=False)
    entity = p4runtime_pb2.Entity()
    table = entity.table_entry
    table.table_id = index.table_id(TABLE_IPV4)
    mf = table.match.add()
    mf.field_id = index.match_field_id(TABLE_IPV4, "dstAddr")
    mf.lpm.value = encode_ipv4(str(network.network_address))
    mf.lpm.prefix_len = int(network.prefixlen)
    if kind != "DELETE":
        table.action.action_profile_group_id = int(entry["group_id"])
    return _update(entity, kind)


def table_entity(
    index: P4InfoIndex,
    table_name: str,
    *,
    match: dict[str, int | str] | None = None,
    action: str | None = None,
    params: dict[str, int | str] | None = None,
    default: bool = False,
    priority: int = 0,
    kind: str = "INSERT",
):
    """Build one direct table entry.

    Keys are ints, dotted IPv4, ``a.b.c.d/len`` (LPM) or ``value&&&mask``
    (ternary, which also needs a *priority*).
    """
    entity = p4runtime_pb2.Entity()
    entry = entity.table_entry
    entry.table_id = index.table_id(table_name)
    entry.is_default_action = bool(default)
    entry.priority = int(priority)
    for field, value in (match or {}).items():
        mf = entry.match.add()
        mf.field_id = index.match_field_id(table_name, field)
        width = index.match_field_bitwidth(table_name, field)
        if isinstance(value, str) and "&&&" in value:
            raw_value, raw_mask = value.split("&&&")
            mf.ternary.value = _encode_value(raw_value, width)
            mf.ternary.mask = _encode_value(raw_mask, width)
        elif isinstance(value, str) and "/" in value:
            network = ipaddress.IPv4Network(value, strict=False)
            mf.lpm.value = encode_ipv4(str(network.network_address))
            mf.lpm.prefix_len = int(network.prefixlen)
        else:
            mf.exact.value = _encode_value(value, width)
    if action is not None and kind != "DELETE":
        entry.action.action.action_id = index.action_id(action)
        for name, value in (params or {}).items():
            param = entry.action.action.params.add()
            param.param_id = index.action_param_id(action, name)
            param.value = _encode_value(
                value, index.action_param_bitwidth(action, name)
            )
    return _update(entity, kind)


def build_update(index: P4InfoIndex, spec: dict[str, Any]) -> p4runtime_pb2.Update:
    """Turn one JSON update description into a P4Runtime ``Update``."""
    kind = str(spec.get("type", "INSERT")).upper()
    if "member" in spec:
        return member_entity(index, spec["member"], kind=kind)
    if "group" in spec:
        return group_entity(index, spec["group"], kind=kind)
    if "lpm" in spec:
        return lpm_entity(index, spec["lpm"], kind=kind)
    return table_entity(
        index,
        spec["table"],
        match=spec.get("match"),
        action=spec.get("action"),
        params=spec.get("params"),
        default=bool(spec.get("default")),
        priority=int(spec.get("priority") or 0),
        kind=kind,
    )


def write_updates(
    client: SwitchClient,
    updates: list[p4runtime_pb2.Update],
    atomicity: str = "CONTINUE_ON_ERROR",
) -> list[dict[str, str]]:
    """Send one Write and return the per-update status the device reports."""
    statuses = [{"code": "OK", "message": ""} for _ in updates]
    if not updates:
        return statuses
    try:
        client.write(updates, _ATOMICITY[atomicity])
    except grpc.RpcError as exc:
        details = [
            status_pb2.Status.FromString(value).details
            for key, value in exc.trailing_metadata() or []
            if key == "grpc-status-details-bin"
        ]
        if not details or len(details[0]) != len(updates):
            code = exc.code().name if exc.code() else "UNKNOWN"
            for status in statuses:
                status.update(code=code, message=str(exc.details() or ""))
            return statuses
        for status, detail in zip(statuses, details[0]):
            error = p4runtime_pb2.Error()
            detail.Unpack(error)
            status.update(
                code=code_pb2.Code.Name(error.canonical_code),
                message=error.message,
            )
    return statuses


def describe_update(index: P4InfoIndex, update: p4runtime_pb2.Update) -> str:
    """One-line, human-readable form of an update for the controller log."""
    kind = p4runtime_pb2.Update.Type.Name(update.type)
    entity = update.entity
    if entity.HasField("action_profile_member"):
        item = entity.action_profile_member
        params = {p.param_id: p.value for p in item.action.params}
        port = decode_u(params.get(index.action_param_id(ACTION_FWD, "port"), b""))
        dst = decode_mac(params.get(index.action_param_id(ACTION_FWD, "dstAddr"), b""))
        return f"{kind} action_profile_member member_id={item.member_id} port={port} dst_mac={dst}"
    if entity.HasField("action_profile_group"):
        item = entity.action_profile_group
        members = [int(m.member_id) for m in item.members]
        return f"{kind} action_profile_group group_id={item.group_id} members={members}"
    entry = entity.table_entry
    names = {int(t.preamble.id): t for t in index.p4info.tables}
    table = names.get(int(entry.table_id))
    fields = {int(f.id): _short(f.name) for f in table.match_fields} if table else {}
    keys = []
    for mf in entry.match:
        name = fields.get(int(mf.field_id), str(mf.field_id))
        if mf.HasField("lpm"):
            keys.append(f"{name}={decode_prefix(mf.lpm.value, mf.lpm.prefix_len)}")
        elif mf.HasField("exact"):
            keys.append(f"{name}={decode_u(mf.exact.value)}")
    if entry.is_default_action:
        keys.append("default")
    which = entry.action.WhichOneof("type")
    if which == "action_profile_group_id":
        target = f"group_id={entry.action.action_profile_group_id}"
    elif which == "action":
        actions = {int(a.preamble.id): a for a in index.p4info.actions}
        action = actions.get(int(entry.action.action.action_id))
        target = _short(action.preamble.name) if action else "action"
    else:
        target = "-"
    table_name = _short(table.preamble.name) if table else str(entry.table_id)
    return f"{kind} table_entry {table_name} {' '.join(keys)} -> {target}"


def _log(lines: list[str]) -> None:
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")
    except OSError:
        pass


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def controller_write(
    client: SwitchClient,
    index: P4InfoIndex,
    device: str,
    updates: list[p4runtime_pb2.Update],
    atomicity: str = "CONTINUE_ON_ERROR",
) -> list[dict[str, str]]:
    """Write *updates* and record the request and its outcome in the log."""
    statuses = write_updates(client, updates, atomicity)
    failed = sum(1 for status in statuses if status["code"] != "OK")
    stamp = _now()
    lines = [
        f"{stamp} Write device={device} device_id={client.device_id} "
        f"election_id={ELECTION_LOW} atomicity={atomicity} "
        f"updates={len(updates)} failed={failed}"
    ]
    for position, (update, status) in enumerate(zip(updates, statuses)):
        outcome = status["code"]
        if status["message"]:
            outcome += f" ({status['message']})"
        lines.append(
            f"{stamp}   [{position}] {describe_update(index, update)}: {outcome}"
        )
    _log(lines)
    return statuses


def connect(switch: dict[str, Any]) -> SwitchClient:
    return SwitchClient(switch["address"], int(switch["device_id"]))


def apply_switch(
    client: SwitchClient,
    index: P4InfoIndex,
    switch: dict[str, Any],
    p4info: p4info_pb2.P4Info,
    device_config: bytes,
) -> None:
    name = str(switch.get("name") or switch["address"])
    client.set_pipeline(p4info, device_config)
    _log(
        [
            f"{_now()} SetForwardingPipelineConfig device={name} "
            f"device_id={client.device_id} action=VERIFY_AND_COMMIT: OK"
        ]
    )
    updates = [member_entity(index, m) for m in switch["members"]]
    updates.extend(group_entity(index, g) for g in switch["groups"])
    updates.extend(lpm_entity(index, e) for e in switch["ipv4_lpm"])
    failed = [
        status
        for status in controller_write(client, index, name, updates)
        if status["code"] != "OK"
    ]
    if failed:
        raise RuntimeError(f"failed to write forwarding state on {name}: {failed}")
    if "role_id" not in switch:
        return
    runtime_updates = [
        table_entity(
            index,
            "runtime_role",
            action="set_role",
            params={"role": switch["role_id"]},
            default=True,
            kind="MODIFY",
        )
    ]
    for raw_port, threshold in switch.get("ecn", {}).items():
        port = int(raw_port)
        runtime_updates.append(
            table_entity(
                index,
                "ecn_config",
                match={"egress_port": port},
                action="set_ecn_threshold",
                params={"threshold": int(threshold)},
            )
        )
        runtime_updates.append(
            table_entity(
                index,
                "int_mtu_config",
                match={"egress_spec": port},
                action="set_int_mtu",
                params={"mtu": int(switch["int"]["mtu"])},
            )
        )
    for raw_port, mtu in (switch.get("egress_mtu") or {}).items():
        runtime_updates.append(
            table_entity(
                index,
                "egress_mtu",
                match={"egress_spec": int(raw_port)},
                action="set_egress_mtu",
                params={"mtu": int(mtu)},
            )
        )
    if switch.get("role") == "gateway":
        runtime_updates.append(
            table_entity(
                index,
                "int_watchlist",
                match={"dstAddr": "10.0.0.0/8"},
                action="watch_int",
            )
        )
        # Edge ICMP hardening: drop timestamp and address-mask requests
        # (RFC 6918 deprecates the latter).
        for icmp_type in (13, 17):
            runtime_updates.append(
                table_entity(
                    index,
                    "icmp_acl",
                    match={"type": icmp_type, "code": 0},
                    action="drop",
                )
            )
        lb = switch.get("l4_load_balancer") or {}
        if lb:
            vip = lb["vip"]
            runtime_updates.append(
                table_entity(
                    index,
                    "lb_vip",
                    match={
                        "dstAddr": vip["ip"],
                        "dstPort": int(vip["port"]),
                        "protocol": 6,
                    },
                    action="set_lb_vip",
                    params={"version": int(lb["pool_version"])},
                )
            )
            backends = lb["backends"]
            for bucket in range(int(lb["hash"]["buckets"])):
                runtime_updates.append(
                    table_entity(
                        index,
                        "lb_pool",
                        match={
                            "lbPoolVersion": int(lb["pool_version"]),
                            "lbBucket": bucket,
                        },
                        action="set_pool_dip",
                        params={"dip": backends[bucket % len(backends)]["dip"]},
                    )
                )
    failed = [
        (describe_update(index, update), status)
        for update, status in zip(
            runtime_updates, controller_write(client, index, name, runtime_updates)
        )
        if status["code"] != "OK"
    ]
    if failed:
        raise RuntimeError(f"failed to write runtime configuration on {name}: {failed}")


def observed_switch(client: SwitchClient, index: P4InfoIndex) -> dict[str, Any]:
    pipeline = client.get_pipeline()
    # The programs carry no @pkginfo name, so compare the loaded P4Info tables
    # with the P4Info this manager programs.
    loaded = set(pipeline.pop("tables", None) or [])
    missing = sorted({t.preamble.name for t in index.p4info.tables} - loaded)
    pipeline.pop("table_count", None)
    pipeline["missing_tables"] = missing
    pipeline["matches_intent"] = bool(pipeline.get("ok")) and not missing
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

    for table_name in (
        "ecn_config",
        "int_mtu_config",
        "egress_mtu",
        "runtime_role",
        "icmp_acl",
    ):
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
                "keys": [decode_u(m.exact.value) for m in entry.match],
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


def switch_summary(observed: dict[str, Any], mismatches: list[str]) -> dict[str, Any]:
    """Compact per-switch digest printed ahead of the full read output."""
    pipeline = observed.get("pipeline") or {}
    return {
        "pipeline_ok": bool(pipeline.get("ok")),
        "pipeline_matches_intent": bool(pipeline.get("matches_intent")),
        "members": len(observed.get("members") or []),
        "groups": len(observed.get("groups") or []),
        "ipv4_lpm": len(observed.get("ipv4_lpm") or []),
        "mismatches": mismatches,
        "nonzero_counter_ports": {
            kind: len(ports) for kind, ports in observed["counters"].items()
        },
    }


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
    names = args.switches or (
        [args.switch] if args.switch else list(intent["switches"])
    )
    payload = {}
    summary = {}
    for name in names:
        switch = intent["switches"][name]
        client = connect(switch)
        try:
            observed = observed_switch(client, index)
            payload[name] = observed
            payload[name]["mismatches"] = intent_vs_observed(switch, observed)
            summary[name] = switch_summary(observed, payload[name]["mismatches"])
        finally:
            client.close()
    # Summary first: tool output is head/tail truncated, so it must lead.
    print(
        json.dumps({"ok": True, "summary": summary, "switches": payload}, default=str)
    )


def table_rows(
    client: SwitchClient, index: P4InfoIndex, table_name: str
) -> list[dict[str, Any]]:
    """Read every entry of one table with decoded keys, action and params."""
    table = index.tables[table_name]
    fields = {int(f.id): _short(f.name) for f in table.match_fields}
    actions = {int(a.preamble.id): a for a in index.p4info.actions}
    request = p4runtime_pb2.Entity()
    request.table_entry.table_id = index.table_id(table_name)
    rows = []
    for entity in client.read_entities(request):
        if not entity.HasField("table_entry"):
            continue
        entry = entity.table_entry
        match: dict[str, Any] = {}
        for mf in entry.match:
            name = fields.get(int(mf.field_id), str(mf.field_id))
            if mf.HasField("exact"):
                match[name] = decode_u(mf.exact.value)
            elif mf.HasField("lpm"):
                match[name] = decode_prefix(mf.lpm.value, mf.lpm.prefix_len)
            elif mf.HasField("ternary"):
                match[name] = [
                    decode_u(mf.ternary.value),
                    decode_u(mf.ternary.mask),
                ]
        action = actions.get(int(entry.action.action.action_id))
        names = {int(p.id): p.name for p in action.params} if action else {}
        rows.append(
            {
                "match": match,
                "priority": int(entry.priority),
                "action": _short(action.preamble.name) if action else None,
                "params": {
                    names.get(int(p.param_id), str(p.param_id)): decode_u(p.value)
                    for p in entry.action.action.params
                },
            }
        )
    return rows


def cmd_read_table(args: argparse.Namespace) -> None:
    """Dump every entry of one table, keyed by the table name."""
    intent = load_json(args.intent)
    index = P4InfoIndex(load_p4info(args.p4info))
    if args.table not in index.tables:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": f"unknown table {args.table}",
                    "tables": sorted(index.tables),
                }
            )
        )
        return
    client = connect(intent["switches"][args.switch])
    try:
        rows = table_rows(client, index, args.table)
    finally:
        client.close()
    print(json.dumps({"ok": True, "switch": args.switch, "tables": {args.table: rows}}))


def cmd_counters(args: argparse.Namespace) -> None:
    intent = load_json(args.intent)
    p4info = load_p4info(args.p4info)
    index = P4InfoIndex(p4info)
    names = [args.switch] if args.switch else list(intent["switches"])
    payload = {}
    summary = {}
    for name in names:
        client = connect(intent["switches"][name])
        try:
            observed = observed_switch(client, index)
            payload[name] = observed["counters"]
            summary[name] = {
                kind: {
                    "ports": len(ports),
                    "packets": sum(c["packets"] for c in ports.values()),
                }
                for kind, ports in observed["counters"].items()
            }
        finally:
            client.close()
    print(json.dumps({"ok": True, "summary": summary, "counters": payload}))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="P4Runtime fabric manager", fromfile_prefix_chars="@"
    )
    parser.add_argument("--intent", default="/tmp/p4_fabric/intent.json")
    parser.add_argument("--p4info", default="/tmp/p4_fabric/fabric.p4info.txt")
    parser.add_argument("--json", default="/tmp/p4_fabric/fabric.json")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_apply = sub.add_parser("apply")
    p_apply.add_argument("--switch")
    p_apply.set_defaults(func=cmd_apply)

    p_read = sub.add_parser("read")
    selection = p_read.add_mutually_exclusive_group()
    selection.add_argument("--switch")
    selection.add_argument("--switches", nargs="+")
    p_read.set_defaults(func=cmd_read)

    p_table = sub.add_parser("read-table")
    p_table.add_argument("--switch", required=True)
    p_table.add_argument("--table", required=True)
    p_table.set_defaults(func=cmd_read_table)

    p_ctr = sub.add_parser("counters")
    p_ctr.add_argument("--switch")
    p_ctr.set_defaults(func=cmd_counters)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
