"""Deterministic synchronized burst traffic."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import time
from dataclasses import dataclass, field, replace
from typing import Literal

from nika.runtime.base import LabRuntime
from nika.runtime.shared.processes import process_source

_RATE_RE = re.compile(r"^(\d+(?:\.\d+)?)([KMG]?)(?:/\d+)?$", re.IGNORECASE)


def _rate_bps(rate: str) -> int:
    match = _RATE_RE.fullmatch(rate)
    if match is None:
        raise ValueError(f"Unsupported burst rate {rate!r}")
    factor = {"": 1, "K": 1_000, "M": 1_000_000, "G": 1_000_000_000}[
        match.group(2).upper()
    ]
    return int(float(match.group(1)) * factor)


_RAW_UDP_SENDER = """import socket,sys,time
dst,port,bps,size,duration,source_port,burst,start=sys.argv[1:]
port,bps,size,source_port,burst=map(int,(port,bps,size,source_port,burst))
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
s.bind(('',source_port))
payload=b'x'*size
period=size*8*burst/bps
start=float(start)
time.sleep(max(0.0,start-time.time()))
end=time.monotonic()+max(0.0,start+float(duration)-time.time())
while time.monotonic()<end:
    for _ in range(burst):
        try: s.sendto(payload,(dst,port))
        except OSError: pass
    time.sleep(min(period,max(0.0,end-time.monotonic())))
"""
# Without a bound socket the receiver answers every datagram with ICMP port
# unreachable, which doubles the load on the fabric toward the senders.
_RAW_UDP_SINK = """import socket,sys,time
port,duration=int(sys.argv[1]),float(sys.argv[2])
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
s.bind(('',port))
s.settimeout(1.0)
end=time.monotonic()+duration
while time.monotonic()<end:
    try: s.recv(65535)
    except OSError: pass
"""


@dataclass(frozen=True)
class BurstFlow:
    source: str
    destination: str
    protocol: Literal["udp", "tcp"]
    source_port: int
    destination_port: int
    flow_id: str
    source_ip: str | None = None


def flow_id_for_five_tuple(
    source_ip: str,
    destination_ip: str,
    protocol: Literal["udp", "tcp"],
    source_port: int,
    destination_port: int,
) -> str:
    protocol_number = 17 if protocol == "udp" else 6
    identity = (
        f"{source_ip}|{destination_ip}|{protocol_number}|"
        f"{source_port}|{destination_port}"
    ).encode()
    return hashlib.blake2b(identity, digest_size=8).hexdigest()


def build_burst_flows(
    sources: list[str],
    destination: str,
    protocol: Literal["udp", "tcp"],
    seed: int,
    flows_per_source: int = 1,
) -> list[BurstFlow]:
    if flows_per_source < 1:
        raise ValueError("flows_per_source must be positive")
    flows = []
    for source_index, source in enumerate(sources):
        for flow_index in range(flows_per_source):
            index = source_index * flows_per_source + flow_index
            digest = hashlib.blake2b(
                f"{seed}|{source}|{destination}|{protocol}|{flow_index}".encode(),
                digest_size=8,
            ).hexdigest()
            flows.append(
                BurstFlow(
                    source=source,
                    destination=destination,
                    protocol=protocol,
                    source_port=20000 + (int(digest[:4], 16) % 30000),
                    destination_port=5201 + index,
                    flow_id=digest,
                )
            )
    return flows


class BurstTrafficGenerator:
    def __init__(self, runtime: LabRuntime):
        self.runtime = runtime

    def prepare(
        self,
        *,
        sources: list[str],
        destination: str,
        protocol: Literal["udp", "tcp"],
        seed: int,
    ) -> PreparedBurst:
        """Resolve endpoints and start owned receivers before a timed burst."""
        flows, destination_ip = self._resolve_flows(
            sources, destination, protocol, seed, 1
        )
        prepared = PreparedBurst(self.runtime, flows, destination_ip)
        try:
            for flow in flows:
                if (
                    not self.runtime.exec(
                        flow.destination, "command -v iperf3", timeout=10
                    )
                    .strip()
                    .startswith("/")
                ):
                    raise RuntimeError(
                        f"Burst receiver {flow.destination!r} requires iperf3"
                    )
                prepared.launch(
                    flow.destination,
                    [
                        "iperf3",
                        "-s",
                        "-1",
                        "--forceflush",
                        "-p",
                        str(flow.destination_port),
                    ],
                )
            return prepared
        except BaseException:
            prepared.stop()
            raise

    def _resolve_flows(
        self,
        sources: list[str],
        destination: str,
        protocol: Literal["udp", "tcp"],
        seed: int,
        flows_per_source: int,
    ) -> tuple[list[BurstFlow], str]:
        flows = build_burst_flows(
            sources, destination, protocol, seed, flows_per_source=flows_per_source
        )
        # Data-plane address: ``hostname -I`` lists the management IP first on
        # containerlab nodes.
        destination_ip = self.runtime.get_data_plane_host_ip(destination)
        if not destination_ip:
            raise RuntimeError(
                f"Could not resolve an IPv4 address for {destination!r}."
            )
        resolved_flows = []
        for flow in flows:
            source_ip = self.runtime.get_data_plane_host_ip(flow.source)
            if not source_ip:
                raise RuntimeError(
                    f"Could not resolve an IPv4 address for {flow.source!r}."
                )
            resolved_flows.append(
                replace(
                    flow,
                    source_ip=source_ip,
                    flow_id=flow_id_for_five_tuple(
                        source_ip,
                        destination_ip,
                        protocol,
                        flow.source_port,
                        flow.destination_port,
                    ),
                )
            )
        return resolved_flows, destination_ip

    def _enable_tcp_ecn_until(self, host: str, seconds: float) -> None:
        """Enable TCP ECN on *host*, then restore its prior value after *seconds*."""
        prior = self.runtime.exec(
            host, "sysctl -n net.ipv4.tcp_ecn", timeout=10
        ).strip()
        self.runtime.exec(host, "sysctl -w net.ipv4.tcp_ecn=1", timeout=10)
        if prior.isdigit() and prior != "1":
            self.runtime.exec(
                host,
                f"(sleep {seconds:.0f}; sysctl -w net.ipv4.tcp_ecn={prior}) "
                ">/dev/null 2>&1 &",
                timeout=10,
            )

    def run(
        self,
        *,
        sources: list[str],
        destination: str,
        protocol: Literal["udp", "tcp"],
        rate: str,
        packet_size: int,
        duration: int,
        synchronized_start: float,
        seed: int,
        flows_per_source: int = 1,
        raw_udp: bool = False,
    ) -> dict:
        if raw_udp and protocol != "udp":
            raise ValueError("raw_udp requires protocol='udp'")
        if raw_udp:
            bitrate = _rate_bps(rate)
            burst_packets = int(rate.split("/", 1)[1]) if "/" in rate else 1
            if bitrate < 1 or burst_packets < 1 or packet_size < 1 or duration <= 0:
                raise ValueError(
                    "Raw UDP rate, burst count, packet size and duration must be positive"
                )
        flows, destination_ip = self._resolve_flows(
            sources, destination, protocol, seed, flows_per_source
        )
        start_time = max(time.time() + 1.0, synchronized_start)
        sink_pids: list[int] = []
        if raw_udp:
            sink_seconds = max(0.0, start_time - time.time()) + duration + 5
            for flow in flows:
                output = self.runtime.exec(
                    destination,
                    f"python3 -c {shlex.quote(_RAW_UDP_SINK)} "
                    f"{flow.destination_port} {sink_seconds:.0f} "
                    f">/dev/null 2>&1 & echo $!",
                    timeout=10,
                ).strip()
                if output.isdigit():
                    sink_pids.append(int(output))
        else:
            for flow in flows:
                self.runtime.exec(
                    destination,
                    f"iperf3 -s -1 -p {flow.destination_port} >/dev/null 2>&1 &",
                    timeout=10,
                )
        if protocol == "tcp":
            restore_after = max(0.0, start_time - time.time()) + duration + 5
            for host in dict.fromkeys([destination, *(f.source for f in flows)]):
                self._enable_tcp_ecn_until(host, restore_after)
        sender_pids: dict[str, int] = {}
        for flow in flows:
            udp = "-u" if protocol == "udp" else ""
            delay = max(0.0, start_time - time.time())
            if raw_udp:
                command = (
                    f"exec python3 -c {shlex.quote(_RAW_UDP_SENDER)} "
                    f"{shlex.quote(destination_ip)} {flow.destination_port} "
                    f"{bitrate} {packet_size} {duration} {flow.source_port} "
                    f"{burst_packets} {start_time:.6f}"
                )
                output = self.runtime.exec(
                    flow.source,
                    f"({command}) >/dev/null 2>&1 & echo $!",
                    timeout=10,
                ).strip()
                if output.isdigit():
                    sender_pids[flow.source] = int(output)
            else:
                command = (
                    f"sleep {delay:.6f}; exec iperf3 -c {destination_ip} -p {flow.destination_port} "
                    f"{udp} -b {rate} -l {packet_size} -t {duration} "
                    f"--cport {flow.source_port}"
                )
                output = self.runtime.exec(
                    flow.source,
                    f"({command}) >/dev/null 2>&1 & echo $!",
                    timeout=10,
                ).strip()
                if output.isdigit():
                    sender_pids[flow.source] = int(output)
        return {
            "event": "traffic_profile",
            "profile": "burst",
            "sources": sources,
            "destination": destination,
            "destination_ip": destination_ip,
            "protocol": protocol,
            "rate": rate,
            "packet_size": packet_size,
            "start_time": start_time,
            "end_time": start_time + duration,
            "seed": seed,
            "flows_per_source": flows_per_source,
            "flows": [flow.__dict__ for flow in flows],
            "sender_pids": sender_pids,
            "sink_pids": sink_pids,
        }


@dataclass
class PreparedBurst:
    """A timed burst's resolved flows and owned receiver/sender processes."""

    runtime: LabRuntime
    flows: list[BurstFlow]
    destination_ip: str
    processes: list[tuple[str, list]] = field(default_factory=list)

    def launch(self, host: str, command: list[str]) -> None:
        """Discard helper output and retain the child's PID and birth identity."""
        source = (
            process_source()
            + """
child = subprocess.Popen(sys.argv[1:], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
try:
    print(json.dumps(process_identity(child.pid)))
except BaseException:
    child.terminate()
    child.wait()
    raise
"""
        )
        identity = json.loads(
            self.runtime.exec(
                host, shlex.join(["python3", "-c", source, *command]), timeout=10
            )
        )
        self.processes.append((host, identity))

    def start(self, *, rate: str, packet_size: int, stop_at: float) -> None:
        _rate_bps(rate)
        for flow in self.flows:
            duration = stop_at - time.monotonic()
            if duration <= 0:
                raise RuntimeError("Burst activation missed its scheduled window")
            self.launch(
                flow.source,
                [
                    "iperf3",
                    "--forceflush",
                    "-c",
                    self.destination_ip,
                    "-p",
                    str(flow.destination_port),
                    *(["-u"] if flow.protocol == "udp" else []),
                    "-b",
                    rate,
                    "-l",
                    str(packet_size),
                    "-t",
                    f"{duration:.6f}",
                    "--cport",
                    str(flow.source_port),
                ],
            )

    def stop(self) -> None:
        error = None
        for host, identity in list(self.processes):
            source = (
                process_source() + f"\nstop_owned({identity!r})\nprint('stopped')\n"
            )
            try:
                output = self.runtime.exec(
                    host, shlex.join(["python3", "-c", source]), timeout=15
                )
                if output.strip() != "stopped":
                    raise RuntimeError(
                        f"Could not stop owned burst process on {host!r}"
                    )
            except BaseException as exc:
                if error is None:
                    error = exc
            else:
                self.processes.remove((host, identity))
        if error is not None:
            raise error
