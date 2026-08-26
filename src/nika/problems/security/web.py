from pydantic import BaseModel, Field

from nika.problems.root_cause import node_resource
from nika.problems.problem_base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.utils.logger import system_logger

# ==================================================================
# Problem: Web service under DoS attack
# ==================================================================


class WebDoSParams(BaseModel):
    """Parameters for injecting a web DoS attack fault."""

    host_name: str = Field(description="Target web server host name.")
    attacker_device: str = Field(description="Attacker host name.")


class WebDoS(ProblemBase):
    failure_domain = FailureDomain.SECURITY
    root_cause_name: str = "web_dos_attack"
    symptom_desc: str = "Users reports high latency when accessing some web services."
    TAGS: str = ["http"]

    Params = WebDoSParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: WebDoSParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: WebDoSParams):
        web_server = params.host_name
        attacker = params.attacker_device
        target_ip = self.runtime.get_host_ip(web_server, with_prefix=False)
        cmd = (
            f"nohup bash -c 'while true; do ab -n 200000000 -c 1000 -k http://{target_ip}/; done'"
            f" </dev/null >/dev/null 2>&1 &"
        )
        self.runtime.exec(attacker, cmd)

    def verify_fault(self, params: WebDoSParams) -> dict:
        """Verify the ab attack process is running on the attacker device."""
        web_server = params.host_name
        attacker = params.attacker_device
        target_ip = self.runtime.get_host_ip(web_server, with_prefix=False)
        pgrep_output = self.runtime.exec(
            attacker, "pgrep -a ab 2>/dev/null || echo NONE"
        ).strip()
        verified = "ab" in pgrep_output and pgrep_output != "NONE"
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "attacker": attacker,
                "target_ip": target_ip,
                "pgrep_output": pgrep_output,
            },
        )
