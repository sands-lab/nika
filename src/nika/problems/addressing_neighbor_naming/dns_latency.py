from pydantic import BaseModel, Field

from nika.problems.base import (
    FailureDomain,
    ProblemBase,
    build_verify_result,
)
from nika.problems.rca.inventory import interface_on
from nika.problems.support.probe_paths import get_probe_path

_RESOLV_BACKUP = "/etc/resolv.conf.nika_bak"


class DNSLookupLatencyParams(BaseModel):
    """Parameters for injecting a DNS lookup latency fault."""

    host_name: str = Field(description="Target DNS server host name.")
    intf_name: str = Field(default="eth0", description="Interface name.")
    delay_ms: int = Field(default=1000, description="Delay in milliseconds.")


class DNSLookupLatency(ProblemBase):
    failure_domain = FailureDomain.ADDRESSING_NEIGHBOR_NAMING
    root_cause_name: str = "dns_lookup_latency"
    description = "DNS lookups are abnormally slow."
    symptom_desc: str = "Users experience high latency when accessing web services."
    TAGS: str = ["dns", "http"]

    Params = DNSLookupLatencyParams

    def __init__(self, scenario_name: str = "dc_clos", **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._pinned_clients: list[str] = []

    def root_cause_resources(self, params: DNSLookupLatencyParams):
        return [interface_on(self.net_env, params.host_name, params.intf_name)]

    def _probe_clients(self) -> list[str]:
        path = get_probe_path(self.scenario_name or "dc_clos")
        clients: list[str] = []
        if path is not None and path.src_host:
            clients.append(path.src_host)
        # Multi-NS resolv.conf is written on every external client; pin the
        # known probe client so alternate nameservers cannot bypass the delay.
        return list(dict.fromkeys(clients))

    def _pin_clients_to_dns(self, params: DNSLookupLatencyParams) -> None:
        dns_ip = self.runtime.get_host_ip(params.host_name, with_prefix=False)
        if not dns_ip:
            return
        self._pinned_clients = []
        for client in self._probe_clients():
            self.runtime.exec(
                client,
                f"cp -a /etc/resolv.conf {_RESOLV_BACKUP} 2>/dev/null || true; "
                f"printf 'nameserver {dns_ip}\\n' > /etc/resolv.conf",
            )
            self._pinned_clients.append(client)

    def _restore_client_resolv(self) -> None:
        for client in self._pinned_clients:
            self.runtime.exec(
                client,
                f"if [ -f {_RESOLV_BACKUP} ]; then "
                f"cp -a {_RESOLV_BACKUP} /etc/resolv.conf; "
                f"rm -f {_RESOLV_BACKUP}; fi",
            )
        self._pinned_clients = []

    def inject_fault(self, params: DNSLookupLatencyParams):
        self.runtime.tc_set_netem(
            params.host_name, params.intf_name, delay_ms=params.delay_ms
        )
        self._pin_clients_to_dns(params)

    def verify_fault(self, params: DNSLookupLatencyParams) -> dict:
        """Verify tc qdisc on DNS server interface has a delay configured."""
        tc_output = self.runtime.exec(
            params.host_name, f"tc qdisc show dev {params.intf_name}"
        ).strip()
        verified = "delay" in tc_output
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "intf": params.intf_name,
                "tc_output": tc_output,
                "pinned_clients": list(self._pinned_clients),
            },
        )

    def recover_fault(self, params: DNSLookupLatencyParams) -> dict:
        self.runtime.tc_clear_intf(params.host_name, params.intf_name)
        self._restore_client_resolv()
        tc_output = self.runtime.exec(
            params.host_name, f"tc qdisc show dev {params.intf_name}"
        ).strip()
        verified = "delay" not in tc_output
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "tc_output": tc_output},
        )
