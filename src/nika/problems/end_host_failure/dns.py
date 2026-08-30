from typing import Optional

from pydantic import BaseModel, Field

from nika.problems.root_cause import node_resource
from nika.problems.problem_base import (
    RootCauseCategory,
    build_verify_result,
    ProblemBase,
)
from nika.utils.logger import system_logger

logger = system_logger

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
    root_cause_category: RootCauseCategory = RootCauseCategory.END_HOST_FAILURE
    root_cause_name: str = "dns_record_error"

    symptom_desc = "Some hosts cannot access external websites."
    TAGS: str = ["dns"]

    Params = DNSRecordErrorParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._wrong_ip: str | None = None

    def root_cause_resources(self, params: DNSRecordErrorParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: DNSRecordErrorParams):
        wrong_ip = params.wrong_ip or self.runtime.get_host_ip(self.net_env.hosts[0])
        self._wrong_ip = wrong_ip
        right_ip = self.runtime.get_host_ip(params.host_name)

        self.runtime.exec(
            params.host_name,
            f"cp /etc/bind/db.{params.target_domain} /etc/bind/db.{params.target_domain}.bak",
        )
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
        wrong_ip = params.wrong_ip or self._wrong_ip
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
            root_cause_name=self.root_cause_name,
            faulty_devices=self.faulty_devices,
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
