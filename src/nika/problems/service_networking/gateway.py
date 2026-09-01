"""Evidence-backed gateway and NAT resource failures."""

from __future__ import annotations

import base64
import time
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource
from nika.problems.support.p4_gateway import (
    delete_lb_conn,
    exhaust_lb_conn_table,
    learn_lb_conn,
    lb_state,
    unsafe_lb_pool_update,
)

_AFFINITY_SCRIPT = r"""import socket
import sys
import time

port = int(sys.argv[1])
vip = sys.argv[2]
vport = int(sys.argv[3])
status_path = sys.argv[4]
try:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", port))
    sock.settimeout(2.0)
    sock.connect((vip, vport))
    request = (
        b"GET / HTTP/1.1\r\nHost: "
        + vip.encode()
        + b"\r\nConnection: keep-alive\r\n\r\n"
    )
    sock.sendall(request)
    sock.recv(4096)
    deadline = time.time() + 45
    while time.time() < deadline:
        try:
            sock.sendall(request)
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("connection closed")
        except OSError:
            with open(status_path, "w", encoding="utf-8") as handle:
                handle.write("broken")
            sys.exit(1)
        time.sleep(0.5)
    with open(status_path, "w", encoding="utf-8") as handle:
        handle.write("ok")
except Exception:
    with open(status_path, "w", encoding="utf-8") as handle:
        handle.write("broken")
    sys.exit(1)
"""


def _parse_vip(vip_url: str) -> tuple[str, int]:
    parsed = urlparse(vip_url)
    host = parsed.hostname or "20.0.0.1"
    port = parsed.port or 80
    return host, port


class LbConnectionStateExhaustionParams(BaseModel):
    host_name: str = Field(default="gateway_1")
    client_host: str = Field(default="client_1")
    vip_url: str = Field(default="http://20.0.0.1/")
    backend_dip: str = Field(default="10.0.1.11")
    attacker_device: str | None = Field(default=None)
    capacity: int = Field(default=256, ge=32, le=256)
    syn_timeout_sec: int = Field(default=10, ge=1)
    seed: int = Field(default=1)


class LbConnectionStateExhaustion(ProblemBase):
    failure_domain = FailureDomain.SERVICE_NETWORKING
    root_cause_name = "lb_connection_state_exhaustion"
    description = "Load-balancer connection-state table is exhausted."
    symptom_desc = "SYN-only state exhausts the gateway ConnTable; pool churn then breaks evicted legitimate connections."
    TAGS = ["p4", "p4_runtime", "http", "l4_load_balancer"]
    Params = LbConnectionStateExhaustionParams

    def root_cause_resources(self, params: LbConnectionStateExhaustionParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: LbConnectionStateExhaustionParams):
        vip_ip, vip_port = _parse_vip(params.vip_url)
        client_ip = self.runtime.get_host_ip(params.client_host, with_prefix=False)
        src_port = 41000 + (params.seed % 900)
        status_path = "/tmp/nika-lb-conn.status"
        pid_path = "/tmp/nika-lb-conn.pid"
        self.runtime.exec(
            params.client_host,
            f"rm -f {status_path} {pid_path}",
        )

        learn_lb_conn(
            self.runtime,
            params.host_name,
            src_addr=client_ip,
            src_port=src_port,
            dst_addr=vip_ip,
            dst_port=vip_port,
            dip=params.backend_dip,
        )

        script_path = "/tmp/nika-lb-affinity.py"
        encoded = base64.b64encode(_AFFINITY_SCRIPT.encode()).decode()
        self.runtime.exec(
            params.client_host,
            f"printf '%s' {encoded} | base64 -d > {script_path}",
        )
        self.runtime.exec(
            params.client_host,
            f"python3 {script_path} {src_port} {vip_ip} {vip_port} {status_path} "
            f">/tmp/nika-lb-conn.log 2>&1 & echo $! > {pid_path}",
            timeout=10,
        )
        time.sleep(0.5)

        fake_count = params.capacity - 1
        exhaust_lb_conn_table(
            self.runtime, params.host_name, fake_count, offset_start=0
        )
        delete_lb_conn(
            self.runtime,
            params.host_name,
            src_addr=client_ip,
            src_port=src_port,
            dst_addr=vip_ip,
            dst_port=vip_port,
        )
        exhaust_lb_conn_table(
            self.runtime, params.host_name, 1, offset_start=fake_count
        )
        self._update = unsafe_lb_pool_update(self.runtime, params.host_name)

        if params.attacker_device:
            syn_duration = min(params.syn_timeout_sec, 5)
            self.runtime.exec(
                params.attacker_device,
                f"timeout {syn_duration}s hping3 -S -p {vip_port} -i u10000 "
                f"{vip_ip} >/dev/null 2>&1 || true",
                timeout=syn_duration + 5,
            )

        time.sleep(min(2.0, float(params.syn_timeout_sec)))
        deadline = time.time() + float(params.syn_timeout_sec)
        while time.time() < deadline:
            status = self.runtime.exec(
                params.client_host,
                f"cat {status_path} 2>/dev/null || true",
            ).strip()
            if status == "broken":
                break
            time.sleep(0.5)
        self._affinity_profile = {
            "client_host": params.client_host,
            "client_ip": client_ip,
            "src_port": src_port,
            "vip_ip": vip_ip,
            "vip_port": vip_port,
            "vip_url": params.vip_url,
            "backend_dip": params.backend_dip,
            "status_path": status_path,
            "pid_path": pid_path,
        }

    def verify_fault(self, params: LbConnectionStateExhaustionParams) -> dict:
        state = lb_state(self.runtime, params.host_name)
        tables = state.get("tables", {})
        return build_verify_result(
            self.root_cause_name,
            tables.get("lb_conn_table") == params.capacity
            and tables.get("lb_pool", 0) >= 128,
            {"gateway": params.host_name, "tables": tables},
        )


class LbPendingConnectionUpdateRaceParams(BaseModel):
    host_name: str = Field(default="gateway_1")
    learning_delay_ms: int = Field(default=5, ge=1, le=100)
    seed: int = Field(default=1)


class LbPendingConnectionUpdateRace(ProblemBase):
    failure_domain = FailureDomain.SERVICE_NETWORKING
    root_cause_name = "lb_pending_connection_update_race"
    description = "Pending connection races an unsafe load-balancer pool update."
    symptom_desc = "An unsafe DIP pool update changes the selected backend before a pending connection reaches ConnTable."
    TAGS = ["p4", "p4_runtime", "http", "l4_load_balancer"]
    Params = LbPendingConnectionUpdateRaceParams

    def root_cause_resources(self, params: LbPendingConnectionUpdateRaceParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: LbPendingConnectionUpdateRaceParams):
        self._state = unsafe_lb_pool_update(self.runtime, params.host_name)

    def verify_fault(self, params: LbPendingConnectionUpdateRaceParams) -> dict:
        state = lb_state(self.runtime, params.host_name)
        tables = state.get("tables", {})
        return build_verify_result(
            self.root_cause_name,
            tables.get("lb_vip") == 1 and tables.get("lb_pool", 0) >= 128,
            {"gateway": params.host_name, "tables": tables},
        )


class SnatPortPoolExhaustionParams(BaseModel):
    host_name: str = Field(default="br1_edge")
    source_prefix: str = Field(default="10.1.40.0/24")
    public_ip: str
    port_start: int = Field(default=40000, ge=1024, le=65535)
    port_end: int = Field(default=40063, ge=1024, le=65535)


class SnatPortPoolExhaustion(ProblemBase):
    failure_domain = FailureDomain.SERVICE_NETWORKING
    root_cause_name = "snat_port_pool_exhaustion"
    description = "SNAT source-port pool is exhausted."
    symptom_desc = "The available SNAT source-port pool is exhausted for concurrent outbound connections."
    TAGS = ["vpn", "http", "nat"]
    Params = SnatPortPoolExhaustionParams

    def root_cause_resources(self, params: SnatPortPoolExhaustionParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: SnatPortPoolExhaustionParams):
        if params.port_end < params.port_start:
            raise ValueError("port_end must be greater than or equal to port_start")
        self.runtime.exec(
            params.host_name,
            "nft add chain ip nat nika_snat_pool "
            "'{ type nat hook postrouting priority 99 ; policy accept; }' 2>/dev/null || true",
        )
        self.runtime.exec(params.host_name, "nft flush chain ip nat nika_snat_pool")
        self.runtime.exec(
            params.host_name,
            f"nft add rule ip nat nika_snat_pool ip protocol tcp "
            f"ip saddr {params.source_prefix} "
            f"snat to {params.public_ip}:{params.port_start}-{params.port_end} "
            "comment 'nika-snat-pool'",
        )

    def verify_fault(self, params: SnatPortPoolExhaustionParams) -> dict:
        rules = self.runtime.exec(params.host_name, "nft -a list table ip nat")
        wanted = f"{params.public_ip}:{params.port_start}-{params.port_end}"
        return build_verify_result(
            self.root_cause_name,
            wanted in rules,
            {"edge": params.host_name, "rules": rules},
        )


class NatMappingRemovedWithoutDrainParams(BaseModel):
    host_name: str = Field(default="br1_edge")
    source_prefix: str = Field(default="10.1.40.0/24")
    nat_ip_a: str
    nat_ip_b: str
    wan_interface: str = Field(
        description="Edge interface that owns the SNAT addresses."
    )


class NatMappingRemovedWithoutDrain(ProblemBase):
    failure_domain = FailureDomain.SERVICE_NETWORKING
    root_cause_name = "nat_mapping_removed_without_drain"
    description = "NAT mapping is removed before active flows drain."
    symptom_desc = (
        "An active SNAT address is removed without draining its conntrack mappings."
    )
    TAGS = ["vpn", "http", "nat"]
    Params = NatMappingRemovedWithoutDrainParams

    def root_cause_resources(self, params: NatMappingRemovedWithoutDrainParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: NatMappingRemovedWithoutDrainParams):
        self._before = self.runtime.exec(
            params.host_name,
            f"ip -4 addr show dev {params.wan_interface}; "
            f"conntrack -L -p tcp --reply-dst {params.nat_ip_a} -o extended 2>/dev/null || true",
        )
        self.runtime.exec(
            params.host_name,
            "nft add chain ip nat nika_nat_failover "
            "'{ type nat hook postrouting priority 98 ; policy accept; }' 2>/dev/null || true; "
            "nft flush chain ip nat nika_nat_failover; "
            f"nft add rule ip nat nika_nat_failover ip protocol tcp ip saddr {params.source_prefix} "
            f"snat to {params.nat_ip_b} comment 'nika-nat-failover'; "
            f"ip addr del {params.nat_ip_a}/32 dev {params.wan_interface}",
        )
        self.runtime.exec(
            params.host_name,
            f"conntrack -D -p tcp --reply-dst {params.nat_ip_a} 2>/dev/null || true",
        )
        # Packets already in flight can otherwise create an UNREPLIED reverse
        # tuple after the address disappears.  It is not an active SNAT
        # mapping, but removing it keeps the tracking state consistent with an
        # immediate, non-draining address withdrawal.
        self.runtime.exec(
            params.host_name,
            f"conntrack -D -p tcp --orig-dst {params.nat_ip_a} 2>/dev/null || true",
        )

    def verify_fault(self, params: NatMappingRemovedWithoutDrainParams) -> dict:
        addresses = self.runtime.exec(
            params.host_name, f"ip -4 addr show dev {params.wan_interface}"
        )
        entries = self.runtime.exec(
            params.host_name,
            f"conntrack -L -p tcp --reply-dst {params.nat_ip_a} -o extended 2>/dev/null || true",
        )
        return build_verify_result(
            self.root_cause_name,
            params.nat_ip_a not in addresses
            and params.nat_ip_b in addresses
            and not entries.strip(),
            {
                "edge": params.host_name,
                "before": getattr(self, "_before", ""),
                "addresses": addresses,
                "conntrack": entries,
            },
        )
