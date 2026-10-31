from pydantic import BaseModel, Field

from nika.problems.base import (
    FailureDomain,
    ProblemBase,
    build_verify_result,
)
from nika.problems.addressing_neighbor_naming.dns import (
    _parse_web_url,
    answer_ip,
)
from nika.problems.rca import node_resource
from nika.problems.support.dns import dns_host_for_domain
from nika.problems.support.oracle import (
    Check,
    OraclePlaybook,
    all_of,
    contains,
    numbers_after,
    qdisc,
    shell,
)
from nika.problems.support.benchmark_targets import (
    all_device_names,
    choice_interface,
    prefer_named,
)


class DNSLookupLatencyParams(BaseModel):
    """Parameters for injecting a DNS lookup latency fault."""

    host_name: str = Field(description="Target DNS server host name.")
    intf_name: str = Field(default="eth0", description="Interface name.")
    delay_ms: int = Field(default=1000, description="Delay in milliseconds.")


class DNSLookupLatency(ProblemBase):
    failure_domain = FailureDomain.ADDRESSING_NEIGHBOR_NAMING
    symptom_probe = "custom"
    symptom_class = "latency"
    root_cause_name: str = "dns_lookup_latency"
    description = "DNS lookups are abnormally slow."
    symptom_desc: str = "Users experience high latency when accessing web services."
    TAGS: str = ["dns", "http"]

    Params = DNSLookupLatencyParams

    BENCHMARK_TARGETS = "canonical"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        dns_list = list(ctx.servers.get("dns") or [])
        if ctx.scenario == "dc_clos":
            dns_target = prefer_named(dns_list, "dns_pod0", ctx.dns0)
        elif ctx.scenario == "campus_lan":
            dns_target = prefer_named(dns_list, "dns_server", ctx.dns0)
        else:
            dns_target = (
                ctx.dns0 if ctx.dns0 in all_device_names(ctx.net_env) else ctx.host0
            )
        return {
            "host_name": dns_target,
            "intf_name": choice_interface(
                ctx.rng, ctx.net_env, dns_target, ctx.backend
            ),
            "delay_ms": "1000",
        }

    def __init__(self, scenario_name: str = "dc_clos", **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: DNSLookupLatencyParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: DNSLookupLatencyParams):
        # Only the DNS server interface is mutated. Clients keep their own
        # resolv.conf; the target server is already their first (or only)
        # nameserver in every compatible scenario.
        self.runtime.tc_set_netem(
            params.host_name, params.intf_name, delay_ms=params.delay_ms
        )

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
            },
        )

    def recover_fault(self, params: DNSLookupLatencyParams) -> dict:
        self.runtime.tc_clear_intf(params.host_name, params.intf_name)
        tc_output = self.runtime.exec(
            params.host_name, f"tc qdisc show dev {params.intf_name}"
        ).strip()
        verified = "delay" not in tc_output
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "tc_output": tc_output},
        )

    def oracle_playbook(self, params: DNSLookupLatencyParams) -> OraclePlaybook:
        dns, intf = params.host_name, params.intf_name
        name = next(
            (
                f"{site}.{domain}"
                for site, domain in map(
                    _parse_web_url, getattr(self.net_env, "web_urls", None) or []
                )
                if dns_host_for_domain(self.net_env, domain) == dns
            ),
            "web0.local",
        )
        client = next(iter(self.net_env.hosts or []), dns)
        slow_ms = params.delay_ms / 2

        def slow_answer(o: str) -> bool:
            times = numbers_after("Query time:", o)
            return bool(answer_ip(o, name)) and bool(times) and times[0] >= slow_ms

        return OraclePlaybook(
            finding=(
                f"Egress on {dns}:{intf} is delayed by about {params.delay_ms} ms, "
                "so every lookup it answers is slow although the records are right."
            ),
            steps=[
                shell(
                    client,
                    f"grep -E '^nameserver' /etc/resolv.conf; "
                    f"dig +time=5 +tries=1 {name} 2>&1 "
                    "| grep -E 'status:|IN\\s+A|Query time|SERVER'",
                    f"Time a lookup of {name} from {client} through its resolver.",
                    all_of(
                        contains(r"status: NOERROR", "the lookup succeeds"),
                        Check(
                            f"the answer takes at least {slow_ms:.0f} ms",
                            slow_answer,
                        ),
                    ),
                    evidence=True,
                    attempts=2,
                ),
                shell(
                    dns,
                    f"dig +time=2 +tries=1 @127.0.0.1 {name} 2>&1 "
                    "| grep -E 'status:|IN\\s+A|Query time'",
                    f"Time the same lookup locally on {dns}: the server itself answers fast.",
                    contains(
                        r"Query time: \d{1,2} msec", "a local answer under 100 ms"
                    ),
                ),
                qdisc(
                    dns,
                    intf,
                    contains(r"netem .*delay \d", "a netem delay qdisc is installed"),
                    f"Inspect the traffic-control queue on {dns}:{intf}.",
                ),
            ],
        )
