import random
from typing import Optional

from pydantic import BaseModel, Field

from nika.problems.rca import node_resource
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.support.benchmark_targets import (
    choice,
)
from nika.utils.logger import system_logger

logger = system_logger


def _parse_web_url(url: str) -> tuple[str, str]:
    website = url.split(".")[0]
    if website.startswith("http://"):
        website = website[len("http://") :]
    domain = url.split(".")[1] if "." in url else "local"
    return website, domain


def _dns_host_for_domain(
    net_env, domain: str, fallback: str | None = None
) -> str | None:
    """Return the DNS server that serves ``domain`` (e.g. dns_pod1 -> pod1)."""
    dns_servers = list((getattr(net_env, "servers", None) or {}).get("dns") or [])
    if not dns_servers:
        return fallback
    if len(dns_servers) == 1:
        return dns_servers[0]
    for host in dns_servers:
        # dc_clos: dns_pod{N} owns zone pod{N}
        if host.startswith("dns_") and host[len("dns_") :] == domain:
            return host
    return fallback or dns_servers[0]


def _dns_record_targets(net_env, rng: random.Random) -> tuple[str, str]:
    urls = getattr(net_env, "web_urls", None) or []
    if urls:
        return _parse_web_url(rng.choice(urls))
    web_pool = net_env.servers.get("web") or []
    web = choice(rng, web_pool, "web0")
    if web:
        return web.replace("web_server_", "web"), "local"
    return "web0", "local"


def _align_dns_record_inject(net_env, row: dict[str, str]) -> dict[str, str]:
    """When host is dns_<zone> and zone exists in web_urls, keep domain/website aligned."""
    out = dict(row)
    host = out.get("host_name") or ""
    if not host.startswith("dns_"):
        return out
    candidate = host[len("dns_") :]
    urls = getattr(net_env, "web_urls", None) or []
    # dns_server must not become domain "server"; only remap for real zones (e.g. pod0).
    match = next((url for url in sorted(urls) if candidate in url), None)
    if match is None:
        return out
    website, domain = _parse_web_url(match)
    out["target_domain"] = domain
    out["target_website"] = website
    return out


# ==================================================================
# Problem: DNS record error. Apps resolve domain but connect to wrong host.
# ==================================================================


class DNSRecordErrorParams(BaseModel):
    """Parameters for injecting a DNS record error fault."""

    host_name: str = Field(description="Target DNS server host name.")
    target_website: str = Field(description="Record host label.")
    target_domain: str = Field(description="DNS zone/domain.")
    wrong_ip: Optional[str] = Field(
        default=None,
        description="Incorrect IP to set. Derived at inject time if omitted.",
    )


class DNSRecordError(ProblemBase):
    failure_domain = FailureDomain.ADDRESSING_NEIGHBOR_NAMING
    root_cause_name: str = "dns_record_error"

    description = "DNS returns an incorrect record for a name."
    symptom_desc = "Some hosts cannot access external websites."
    TAGS: str = ["dns"]

    Params = DNSRecordErrorParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        website, domain = _dns_record_targets(ctx.net_env, ctx.rng)
        return {
            "target_website": website,
            "target_domain": domain,
            "host_name": (
                _dns_host_for_domain(ctx.net_env, domain, fallback=ctx.dns0) or ctx.dns0
            ),
        }

    @classmethod
    def benchmark_align_option(cls, ctx, row, field):
        if field != "host_name":
            return row
        return _align_dns_record_inject(ctx.net_env, row)

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        website = inject.get("target_website", "")
        domain = inject.get("target_domain", "")
        host_name = inject.get("host_name", "")
        urls = getattr(ctx.net_env, "web_urls", None) or []
        if urls:
            matched = any(
                website in url and (not domain or domain in url) for url in urls
            )
            if not matched:
                raise ValueError(
                    f"DNS record targets {website}.{domain} not found in web_urls for {ctx.scenario}: {urls}"
                )
        expected_host = _dns_host_for_domain(ctx.net_env, domain)
        if expected_host and host_name and host_name != expected_host:
            raise ValueError(
                f"DNS host {host_name!r} does not serve zone {domain!r} on {ctx.scenario}; "
                f"expected {expected_host!r}"
            )

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._wrong_ip: str | None = None

    def root_cause_resources(self, params: DNSRecordErrorParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: DNSRecordErrorParams):
        if params.wrong_ip:
            wrong_ip = params.wrong_ip
        else:
            fallback_host = params.host_name
            hosts = getattr(self.net_env, "hosts", None) or []
            if hosts and hosts[0]:
                fallback_host = hosts[0]
            wrong_ip = self.runtime.get_host_ip(fallback_host)
        self._wrong_ip = wrong_ip
        right_ip = self.runtime.get_host_ip(params.host_name)

        # No backup copy: a db.*.bak next to the zone file exposes the original
        # record and the injected server.
        cmd = r"sed -i 's/^\({name}[[:space:]]\+IN[[:space:]]\+A[[:space:]]\+\)[0-9\.]\+/\1{new_ip}/' /etc/bind/db.{domain}"
        cmd = cmd.format(
            name=params.target_website, new_ip=wrong_ip, domain=params.target_domain
        )
        self.runtime.exec(params.host_name, cmd)
        self.runtime.exec(
            params.host_name,
            "rndc reload 2>/dev/null || service named restart 2>/dev/null || true",
        )
        logger.info(
            f"Injecting DNS record error on {params.host_name}: mapping {params.target_website}:{params.target_domain} "
            f"to wrong IP {wrong_ip} instead of {right_ip}"
        )

    def verify_fault(self, params: DNSRecordErrorParams) -> dict:
        """Verify the DNS zone file contains the wrong IP and the running daemon serves it."""
        wrong_ip = params.wrong_ip or getattr(self, "_wrong_ip", None)
        if not wrong_ip and self.net_env.hosts:
            # Fresh problem instance after workflow inject: re-derive the same way.
            wrong_ip = self.runtime.get_host_ip(self.net_env.hosts[0])
        if not wrong_ip:
            return build_verify_result(
                fault_type=self.root_cause_name,
                verified=False,
                details={"error": "wrong_ip unavailable"},
            )
        grep_result = self.runtime.exec(
            params.host_name,
            f"grep '{params.target_website}.*{wrong_ip}' /etc/bind/db.{params.target_domain} 2>/dev/null && echo found || echo absent",
        ).strip()
        file_has_wrong_ip = "found" in grep_result
        dig_result = self.runtime.exec(
            params.host_name,
            f"dig +short {params.target_website}.{params.target_domain} @127.0.0.1 2>/dev/null || echo absent",
        ).strip()
        dns_resolves_wrong = wrong_ip in dig_result
        verified = file_has_wrong_ip and dns_resolves_wrong
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "target_website": params.target_website,
                "wrong_ip": wrong_ip,
                "grep_result": grep_result,
                "file_has_wrong_ip": file_has_wrong_ip,
                "dig_result": dig_result,
                "dns_resolves_wrong": dns_resolves_wrong,
            },
        )
