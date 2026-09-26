"""Deterministic synchronized burst traffic."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, replace
from typing import Literal

from nika.runtime.base import LabRuntime


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
    ) -> dict:
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
        start_time = max(time.time() + 1.0, synchronized_start)
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
        flows = resolved_flows
        for flow in flows:
            self.runtime.exec(
                destination,
                f"iperf3 -s -1 -p {flow.destination_port} >/tmp/burst-server-{flow.flow_id}.log 2>&1 &",
                timeout=10,
            )
        if protocol == "tcp":
            restore_after = max(0.0, start_time - time.time()) + duration + 5
            for host in dict.fromkeys([destination, *(f.source for f in flows)]):
                self._enable_tcp_ecn_until(host, restore_after)
        for flow in flows:
            udp = "-u" if protocol == "udp" else ""
            delay = max(0.0, start_time - time.time())
            command = (
                f"sleep {delay:.6f}; iperf3 -c {destination_ip} -p {flow.destination_port} "
                f"{udp} -b {rate} -l {packet_size} -t {duration} "
                f"--cport {flow.source_port} >/tmp/burst-{flow.flow_id}.log 2>&1"
            )
            self.runtime.exec(flow.source, command + " &", timeout=10)
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
        }
