#!/usr/bin/env python3
"""Gateway-local L4 load-balancer agent: connection learning and pool updates.

``serve`` is the switch-CPU learner of a SilkRoad-style stateful load
balancer.  It watches the BMv2 dataplane interfaces: a client SYN to the VIP
opens a *pending* connection, and the translated SYN the pipeline emits tells
which DIP the current pool version chose.  ``learning_delay_ms`` after the SYN
the learner pins that 5-tuple to that DIP in the size-limited
``lb_conn_table``; RST, FIN (after a short grace) and idleness remove it.
Until a connection is learned, its packets are mapped by the pool hash alone.
The delay is read from ``--config``; the live state is written to ``--status``.

``update`` is the routine pool update: it flips the VIP to the other pool
version, whose buckets map to a rotated DIP list.  Learned connections keep
their DIP; pending ones follow the new pool.

The learner holds P4Runtime election id 0, below the fabric manager's, so a
manager connection preempts it and it resumes as primary afterwards; writes
refused while it is a backup are retried.
"""

from __future__ import annotations

import argparse
import ctypes
import heapq
import json
import os
import queue
import socket
import struct
import threading
import time

import grpc
from p4.v1 import p4runtime_pb2, p4runtime_pb2_grpc

ETH_P_ALL = 3
SO_ATTACH_FILTER = 26
PACKET_OUTGOING = 4
TCP_FIN, TCP_SYN, TCP_RST, TCP_ACK = 0x01, 0x02, 0x04, 0x10
FIN_GRACE_SEC = 2.0
PENDING_TIMEOUT_SEC = 10.0
IDLE_TIMEOUT_SEC = 300.0
RETRY_SEC = 0.05
RECONNECT_SEC = 1.0
_RETRYABLE = (
    grpc.StatusCode.PERMISSION_DENIED,  # preempted by a manager
    grpc.StatusCode.UNAVAILABLE,
    grpc.StatusCode.DEADLINE_EXCEEDED,
)
LEARNER_ELECTION_ID = 0
OPERATOR_ELECTION_ID = 1
Update = p4runtime_pb2.Update


def _value(raw: bytes) -> bytes:
    return raw.lstrip(b"\0") or b"\0"


class Switch:
    """One P4Runtime stream to the local BMv2 at a fixed election id."""

    def __init__(self, device_id: int, election_id: int):
        self.device_id = device_id
        self.election_id = election_id
        self.channel = grpc.insecure_channel("127.0.0.1:9559")
        self.stub = p4runtime_pb2_grpc.P4RuntimeStub(self.channel)
        self.requests: queue.Queue = queue.Queue()
        self.alive = True
        self.arbitrated = threading.Event()
        arbitration = p4runtime_pb2.StreamMessageRequest()
        arbitration.arbitration.device_id = device_id
        arbitration.arbitration.election_id.low = election_id
        self.requests.put(arbitration)
        self.stream = self.stub.StreamChannel(iter(self.requests.get, None))
        try:
            self._handshake()
        except BaseException:
            self.close()
            raise

    def _handshake(self) -> None:
        threading.Thread(target=self._listen, daemon=True).start()
        if not self.arbitrated.wait(10):
            raise TimeoutError("P4Runtime arbitration timeout")
        request = p4runtime_pb2.GetForwardingPipelineConfigRequest(
            device_id=self.device_id,
            response_type=p4runtime_pb2.GetForwardingPipelineConfigRequest.P4INFO_AND_COOKIE,
        )
        p4info = self.stub.GetForwardingPipelineConfig(
            request, timeout=10
        ).config.p4info
        self.tables = {
            t.preamble.name.rsplit(".", 1)[-1]: (
                t.preamble.id,
                [f.id for f in sorted(t.match_fields, key=lambda f: f.id)],
            )
            for t in p4info.tables
        }
        self.actions = {
            a.preamble.name.rsplit(".", 1)[-1]: a.preamble.id for a in p4info.actions
        }

    def _listen(self) -> None:
        try:
            for message in self.stream:
                if message.HasField("arbitration"):
                    self.arbitrated.set()
        except grpc.RpcError:
            pass
        self.alive = False
        self.arbitrated.set()

    def entry(
        self, table: str, key: list[bytes], action: str | None = None, arg: bytes = b""
    ):
        table_id, field_ids = self.tables[table]
        entry = p4runtime_pb2.TableEntry(table_id=table_id)
        for field_id, raw in zip(field_ids, key):
            match = entry.match.add()
            match.field_id = field_id
            match.exact.value = _value(raw)
        if action is not None:
            entry.action.action.action_id = self.actions[action]
            param = entry.action.action.params.add()
            param.param_id = 1
            param.value = _value(arg)
        return entry

    def write(self, kind: int, entries: list) -> None:
        request = p4runtime_pb2.WriteRequest(device_id=self.device_id)
        request.election_id.low = self.election_id
        for entry in entries:
            update = request.updates.add()
            update.type = kind
            update.entity.table_entry.CopyFrom(entry)
        self.stub.Write(request, timeout=10)

    def read(self, table: str) -> list:
        request = p4runtime_pb2.ReadRequest(device_id=self.device_id)
        request.entities.add().table_entry.table_id = self.tables[table][0]
        return [
            entity.table_entry
            for response in self.stub.Read(request, timeout=10)
            for entity in response.entities
        ]

    def close(self) -> None:
        self.requests.put(None)
        self.channel.close()


def attach_port_filter(capture: socket.socket, port: int) -> None:
    """Keep only IPv4 TCP frames to or from ``port`` (``tcpdump -dd``)."""
    program = [
        (0x28, 0, 0, 0x0000000C),
        (0x15, 0, 10, 0x00000800),
        (0x30, 0, 0, 0x00000017),
        (0x15, 0, 8, 0x00000006),
        (0x28, 0, 0, 0x00000014),
        (0x45, 6, 0, 0x00001FFF),
        (0xB1, 0, 0, 0x0000000E),
        (0x48, 0, 0, 0x0000000E),
        (0x15, 2, 0, port),
        (0x48, 0, 0, 0x00000010),
        (0x15, 0, 1, port),
        (0x6, 0, 0, 0x00040000),
        (0x6, 0, 0, 0x00000000),
    ]
    code = ctypes.create_string_buffer(
        b"".join(struct.pack("HBBI", *insn) for insn in program)
    )
    fprog = struct.pack("HL", len(program), ctypes.addressof(code))
    capture.setsockopt(socket.SOL_SOCKET, SO_ATTACH_FILTER, fprog)


def parse_tcp(frame: bytes):
    if len(frame) < 34 or frame[12:14] != b"\x08\x00" or frame[23] != 6:
        return None
    off = 14 + (frame[14] & 0x0F) * 4
    if len(frame) < off + 14:
        return None
    sport, dport = struct.unpack("!HH", frame[off : off + 4])
    return (
        socket.inet_ntoa(frame[26:30]),
        socket.inet_ntoa(frame[30:34]),
        sport,
        dport,
        frame[off + 13],
    )


def read_delay(path: str, default: int) -> int:
    try:
        with open(path, encoding="utf-8") as handle:
            return int(json.load(handle)["learning_delay_ms"])
    except (OSError, ValueError, KeyError, TypeError):
        return default


def serve(args: argparse.Namespace) -> None:
    interfaces = set(json.loads(args.ports))
    os.makedirs(os.path.dirname(args.config), exist_ok=True)
    os.makedirs(os.path.dirname(args.status), exist_ok=True)
    if not os.path.exists(args.config):
        with open(args.config, "w", encoding="utf-8") as handle:
            json.dump({"learning_delay_ms": args.learning_delay_ms}, handle)
    delay_ms = read_delay(args.config, args.learning_delay_ms)
    capture = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    attach_port_filter(capture, args.vip_port)
    capture.settimeout(0.02)
    vip = socket.inet_aton(args.vip)
    switch: Switch | None = None
    pending: dict[tuple[str, int], list] = {}  # key -> [syn_time, dip | None]
    learned: dict[tuple[str, int], str] = {}  # key -> dip
    last_seen: dict[tuple[str, int], float] = {}
    # (when, action, key, syn_time): a learn only applies to the SYN that scheduled it.
    due: list[tuple[float, str, tuple[str, int], float]] = []
    stats = {"installs": 0, "insert_failures": 0, "deletes": 0, "last_learn_ms": None}
    next_status = 0.0
    reconnect_at = 0.0

    def conn_key(key: tuple[str, int]) -> list[bytes]:
        return [
            socket.inet_aton(key[0]),
            vip,
            bytes([6]),
            struct.pack("!H", key[1]),
            struct.pack("!H", args.vip_port),
        ]

    def write(kind: int, key: tuple[str, int], dip: str | None = None) -> bool | None:
        """True on success, None to retry later, False to give up."""
        nonlocal switch, reconnect_at
        try:
            if switch is None or not switch.alive:
                if switch is not None:
                    switch.close()
                    switch = None
                if time.time() < reconnect_at:
                    return None
                reconnect_at = time.time() + RECONNECT_SEC
                switch = Switch(args.device_id, LEARNER_ELECTION_ID)
            action = "set_lb_dip" if dip else None
            arg = socket.inet_aton(dip) if dip else b""
            entry = switch.entry("lb_conn_table", conn_key(key), action, arg)
            try:
                switch.write(kind, [entry])
            except grpc.RpcError as exc:
                if kind != Update.INSERT or exc.code() in _RETRYABLE:
                    raise
                # BMv2 reports ALREADY_EXISTS as UNKNOWN with per-update
                # details, so take over a leftover entry; a full table makes
                # the MODIFY fail too.
                switch.write(Update.MODIFY, [entry])
            return True
        except grpc.RpcError as exc:
            if exc.code() in _RETRYABLE:
                if exc.code() != grpc.StatusCode.PERMISSION_DENIED and switch:
                    switch.close()
                    switch = None
                return None
            return False  # table full, entry gone after a pipeline reload
        except KeyError:
            return False  # a different pipeline without the L4 tables
        except Exception:
            if switch is not None:
                switch.close()
            switch = None
            return None

    while True:
        now = time.time()
        try:
            frame, address = capture.recvfrom(65535)
        except socket.timeout:
            frame, address = b"", ("", 0, 0)
        packet = parse_tcp(frame) if address[0] in interfaces else None
        if packet is not None:
            src, dst, sport, dport, flags = packet
            key = (src, sport)
            outgoing = address[2] == PACKET_OUTGOING
            if not outgoing and dst == args.vip and dport == args.vip_port:
                if flags & (TCP_RST | TCP_FIN):
                    grace = 0.0 if flags & TCP_RST else FIN_GRACE_SEC
                    heapq.heappush(due, (now + grace, "forget", key, 0.0))
                elif flags & TCP_SYN and not flags & TCP_ACK:
                    if key not in learned and key not in pending:
                        pending[key] = [now, None]
                if key in pending or key in learned:
                    last_seen[key] = now
            elif (
                outgoing
                and src == args.vip
                and sport == args.vip_port
                and flags & TCP_RST
            ):
                # A backend reset the connection.
                heapq.heappush(due, (now, "forget", (dst, dport), 0.0))
            elif (
                outgoing
                and flags & TCP_SYN
                and not flags & TCP_ACK
                and dport == args.vip_port
                and dst != args.vip
                and key in pending
                and pending[key][1] is None
            ):
                # The pipeline's own translation decision for this SYN.
                pending[key][1] = dst
                started = pending[key][0]
                heapq.heappush(
                    due, (started + delay_ms / 1000.0, "learn", key, started)
                )
        while due and due[0][0] <= now:
            _, action, key, stamp = heapq.heappop(due)
            if (
                action == "learn"
                and key in pending
                and pending[key][0] == stamp
                and pending[key][1] is not None
            ):
                result = write(Update.INSERT, key, pending[key][1])
                if result is None:
                    heapq.heappush(due, (now + RETRY_SEC, action, key, stamp))
                    break
                started, dip = pending.pop(key)
                if result:
                    learned[key] = dip
                    stats["installs"] += 1
                    stats["last_learn_ms"] = round((time.time() - started) * 1000)
                else:
                    stats["insert_failures"] += 1
                    last_seen.pop(key, None)
            elif action == "forget":
                pending.pop(key, None)
                if key in learned:
                    result = write(Update.DELETE, key)
                    if result is None:
                        heapq.heappush(due, (now + RETRY_SEC, action, key, stamp))
                        break
                    del learned[key]
                    stats["deletes"] += int(result)
                last_seen.pop(key, None)
        if now >= next_status:
            next_status = now + 1.0
            delay_ms = read_delay(args.config, delay_ms)
            for key, (started, _dip) in list(pending.items()):
                if now - started > PENDING_TIMEOUT_SEC + delay_ms / 1000.0:
                    pending.pop(key, None)
                    last_seen.pop(key, None)
            for key in learned:
                if now - last_seen.get(key, now) > IDLE_TIMEOUT_SEC:
                    heapq.heappush(due, (now, "forget", key, 0.0))
            status = {
                "pid": os.getpid(),
                "time": now,
                "learning_delay_ms": delay_ms,
                "pending": len(pending),
                "learned": len(learned),
                **stats,
            }
            with open(args.status + ".tmp", "w", encoding="utf-8") as handle:
                json.dump(status, handle)
            os.replace(args.status + ".tmp", args.status)


def update(args: argparse.Namespace) -> None:
    """Flip the VIP to the other pool version (a routine DIP pool update)."""
    switch = Switch(args.device_id, OPERATOR_ELECTION_ID)
    try:
        vip_key = [
            socket.inet_aton(args.vip),
            struct.pack("!H", args.vip_port),
            bytes([6]),
        ]
        current = switch.read("lb_vip")[0].action.action.params[0].value[-1]
        target = 2 if current == 1 else 1
        pools: dict[int, dict[int, bytes]] = {}
        for entry in switch.read("lb_pool"):
            version = entry.match[0].exact.value[-1]
            bucket = entry.match[1].exact.value[-1]
            dip = entry.action.action.params[0].value.rjust(4, b"\0")
            pools.setdefault(version, {})[bucket] = dip
        if target not in pools:
            layout = pools[current]
            dips = sorted(set(layout.values()))
            switch.write(
                Update.INSERT,
                [
                    switch.entry(
                        "lb_pool",
                        [bytes([target]), bytes([bucket])],
                        "set_pool_dip",
                        dips[(dips.index(dip) + 1) % len(dips)],
                    )
                    for bucket, dip in sorted(layout.items())
                ],
            )
        switch.write(
            Update.MODIFY,
            [switch.entry("lb_vip", vip_key, "set_lb_vip", bytes([target]))],
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "from_version": current,
                    "to_version": target,
                    "time": time.time(),
                }
            )
        )
    finally:
        switch.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "update"):
        command = sub.add_parser(name)
        command.add_argument("--device-id", type=int, required=True)
        command.add_argument("--vip", default="20.0.0.1")
        command.add_argument("--vip-port", type=int, default=80)
    sub.choices["serve"].add_argument(
        "--ports", required=True, help="JSON list of dataplane interfaces"
    )
    sub.choices["serve"].add_argument("--learning-delay-ms", type=int, default=5)
    sub.choices["serve"].add_argument("--config", required=True)
    sub.choices["serve"].add_argument("--status", required=True)
    args = parser.parse_args()
    serve(args) if args.command == "serve" else update(args)


if __name__ == "__main__":
    main()
