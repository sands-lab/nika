import random
import re

from pydantic import BaseModel, Field

from nika.problems.rca.inventory import interface_on
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.support.benchmark_targets import (
    choice_distinct,
    require_distinct_hosts,
)
from nika.problems.support.oracle import (
    OraclePlaybook,
    OracleStep,
    all_of,
    contains,
    lacks,
    shell,
)
from nika.utils.logger import system_logger


_MAC_RE = re.compile(r"link/ether ([0-9a-f]{2}(?::[0-9a-f]{2}){5})", re.IGNORECASE)


def _mac_of(output: str) -> str:
    match = _MAC_RE.search(output or "")
    return match.group(1).lower() if match else ""


def _mac_conflict_pair(net_env, rng: random.Random) -> tuple[str, str]:
    """Prefer two distinct endpoint hosts so L2 MAC conflict is observable."""
    hosts = list(net_env.hosts or [])
    servers = net_env.servers or {}
    for bucket in servers.values():
        for name in bucket or []:
            if name not in hosts:
                hosts.append(name)
    if len(hosts) >= 2:
        pair = choice_distinct(rng, hosts, hosts[0])
        return pair[0], pair[1]
    topo = net_env.get_topology()
    if topo:
        link = rng.choice(topo)
        device_a = link[0].split(":")[0]
        device_b = link[1].split(":")[0]
        return device_a, device_b
    pair = choice_distinct(rng, hosts, "pc1")
    return pair[0], pair[1]


# ==================================================================
# Problem: MAC address conflict
# ==================================================================


class MacAddressConflictParams(BaseModel):
    """Parameters for injecting a MAC address conflict fault."""

    host_name: str = Field(description="Target host/device receiving conflicting MAC.")
    host_name_2: str = Field(description="Peer device whose MAC is copied.")


class MacAddressConflict(ProblemBase):
    failure_domain = FailureDomain.ADDRESSING_NEIGHBOR_NAMING
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name: str = "mac_address_conflict"
    root_cause_owner = "interface"
    description = "Two hosts share the same MAC address."
    TAGS: str = ["mac"]

    Params = MacAddressConflictParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        a, b = _mac_conflict_pair(ctx.net_env, ctx.rng)
        return {"host_name": a, "host_name_2": b}

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        require_distinct_hosts(ctx, cls.root_cause_name, inject)

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: MacAddressConflictParams):
        return [interface_on(self.net_env, params.host_name, "eth0")]

    def inject_fault(self, params: MacAddressConflictParams):
        device_0 = params.host_name
        device_1 = params.host_name_2
        target_mac = self.runtime.get_host_mac_address(device_1, "eth0")
        self.runtime.exec(device_0, f"ip link set dev eth0 address {target_mac}")
        self.logger.info(
            f"Injected MAC address conflict on {device_0} with MAC {target_mac} of {device_1}"
        )

    def verify_fault(self, params: MacAddressConflictParams) -> dict:
        """Verify device_0's eth0 MAC matches device_1's eth0 MAC (conflict)."""
        device_0 = params.host_name
        device_1 = params.host_name_2
        mac_0 = self.runtime.get_host_mac_address(device_0, "eth0")
        mac_1 = self.runtime.get_host_mac_address(device_1, "eth0")
        verified = bool(mac_0) and mac_0.lower() == mac_1.lower()
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "device_0": device_0,
                "device_1": device_1,
                "mac_0": mac_0,
                "mac_1": mac_1,
            },
        )

    def oracle_playbook(self, params: MacAddressConflictParams) -> OraclePlaybook:
        host, peer = params.host_name, params.host_name_2

        def peer_mac_on_host(outputs) -> dict:
            mac = _mac_of(outputs.get("peer_link", ""))
            command = (
                f"ip -o link show dev eth0 | grep -io 'link/ether {mac}' "
                "|| echo MAC_DIFFERS"
                if mac
                else "echo PEER_MAC_UNKNOWN"
            )
            return {"host_name": host, "command": command, "timeout": 10}

        return OraclePlaybook(
            finding=(
                f"{host}:eth0 uses the same MAC address as {peer}:eth0, so "
                "switches and neighbors deliver their frames to the wrong host."
            ),
            steps=[
                shell(
                    host,
                    "ip -o link show dev eth0; ip -4 -br addr show dev eth0",
                    f"Read the MAC and address of {host}:eth0.",
                    contains(r"link/ether", "eth0 has a MAC address"),
                ),
                shell(
                    peer,
                    "ip -o link show dev eth0; ip -4 -br addr show dev eth0",
                    f"Read the MAC and address of {peer}:eth0, the host that lost traffic.",
                    contains(r"link/ether", "eth0 has a MAC address"),
                    save_as="peer_link",
                ),
                OracleStep(
                    f"Compare {host}:eth0's MAC with {peer}:eth0's MAC.",
                    "exec_shell",
                    peer_mac_on_host,
                    all_of(
                        contains(r"link/ether", f"{host}:eth0 carries {peer}'s MAC"),
                        lacks(
                            r"MAC_DIFFERS|PEER_MAC_UNKNOWN", "the MACs are identical"
                        ),
                    ),
                    evidence=True,
                ),
            ],
        )
