import random
import re
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
from nika.problems.support.dns import dns_host_for_domain
from nika.problems.support.oracle import (
    Check,
    OraclePlaybook,
    OracleStep,
    all_of,
    contains,
    http_probe,
    numbers_after,
    shell,
)
from nika.utils.logger import system_logger

logger = system_logger


def _parse_web_url(url: str) -> tuple[str, str]:
    website = url.split(".")[0]
    if website.startswith("http://"):
        website = website[len("http://") :]
    domain = url.split(".")[1] if "." in url else "local"
    return website, domain


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


def web_server_for(net_env, website: str, domain: str) -> str | None:
    """Web server node that serves ``<website>.<domain>`` (web1 -> web_server_1)."""
    m = re.search(r"(\d+)$", website)
    if not m:
        return None
    index = m.group(1)
    for node in (getattr(net_env, "servers", None) or {}).get("web") or []:
        rest = node.replace(domain, "") if domain != "local" else node
        if domain != "local" and domain not in node:
            continue
        if re.search(rf"(?<!\d){index}(?!\d)", rest):
            return node
    return None


def answer_ip(output: str, name: str) -> str | None:
    """A-record address that ``dig`` returned for *name*."""
    m = re.search(
        rf"^{re.escape(name)}\.?\s+\d+\s+IN\s+A\s+(\d+\.\d+\.\d+\.\d+)",
        output,
        re.MULTILINE,
    )
    return m.group(1) if m else None


def fast_answer(name: str, max_ms: float) -> Check:
    def predicate(o: str) -> bool:
        times = numbers_after("Query time:", o)
        return bool(answer_ip(o, name)) and bool(times) and times[0] < max_ms

    return Check(f"an A record for {name} returns within {max_ms:.0f} ms", predicate)


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
    symptom_probe = "custom"
    symptom_class = "unreachable"
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
                dns_host_for_domain(ctx.net_env, domain, fallback=ctx.dns0) or ctx.dns0
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
        expected_host = dns_host_for_domain(ctx.net_env, domain)
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

    def oracle_playbook(self, params: DNSRecordErrorParams) -> OraclePlaybook:
        dns = params.host_name
        name = f"{params.target_website}.{params.target_domain}"
        zone = f"/etc/bind/db.{params.target_domain}"
        web = web_server_for(self.net_env, params.target_website, params.target_domain)
        client = next(iter(self.net_env.hosts or []), None)
        steps = []
        if client:
            steps.append(
                http_probe(
                    client,
                    f"http://{name}/",
                    f"Reproduce the user symptom: fetch http://{name}/ by name from {client}.",
                )
            )
        steps.append(
            shell(
                dns,
                f"dig +time=2 +tries=1 @127.0.0.1 {name} 2>&1 | grep -E 'status:|IN\\s+A|Query time'; "
                f"grep -nE '^{re.escape(params.target_website)}[[:space:]]' {zone}",
                f"Ask the authoritative server {dns} for {name} and read its zone record.",
                all_of(
                    contains(r"status: NOERROR", "the server answers"),
                    fast_answer(name, 500),
                    contains(
                        rf"^\d+:{re.escape(params.target_website)}\s+IN\s+A\s+[\d.]+",
                        "the zone file holds the record",
                    ),
                ),
                evidence=True,
                save_as="answer",
            )
        )
        if web:

            def ownership(out):
                ip = answer_ip(out.get("answer", ""), name) or "0.0.0.0"
                return {
                    "host_name": web,
                    "command": (
                        "ip -4 -o addr show scope global | awk '{print $2, $4}'; "
                        f"ip -4 -o addr show scope global | grep -qw '{ip}' "
                        f"&& echo OWNS={ip} || echo NOT_OWNED={ip}; "
                        "curl -s -o /dev/null -m 3 -w 'http_local=%{http_code}\\n' "
                        f"-H 'Host: {name}' http://127.0.0.1/; "
                        # The server still runs the address it was provisioned with.
                        "A=$(ip -4 -o addr show dev eth0 scope global "
                        "| awk '{print $4}' | head -1); "
                        'grep -qF "addr add $A" /var/log/startup.log '
                        "&& echo BOOT_MATCH=$A || echo BOOT_DRIFT=$A"
                    ),
                    "timeout": 15,
                }

            steps.append(
                OracleStep(
                    f"Check that the web server {web} for {name} holds the "
                    "published address and serves HTTP.",
                    "exec_shell",
                    ownership,
                    all_of(
                        contains(r"^NOT_OWNED=", "the A record is not its address"),
                        contains(r"^http_local=[23]\d\d", "its web service is up"),
                        contains(r"^BOOT_MATCH=", "it runs its provisioned address"),
                    ),
                    evidence=True,
                )
            )
        return OraclePlaybook(
            finding=(
                f"The zone on {dns} publishes a wrong A record for {name}, so "
                "clients resolve it quickly but connect to a host that is not "
                "the web server."
            ),
            steps=steps,
        )
