from pydantic import BaseModel, Field

from nika.problems.problem_base import (
    FailureCause,
    FailureDomain,
    FailureImpact,
    FailureScope,
    FailureSymptom,
    FailureTemporal,
    build_verify_result,
    ProblemBase,
)
from nika.problems.topology_inventory import interface_on
from nika.runtime.base import RuntimeCapabilityError
from nika.utils.logger import system_logger

# ==========================================
# Problem: Path MTU / MTU mismatch (oversized packets drop)
# ==========================================
#
# Models an effective path MTU that is too small for large packets: frames
# at or above ``mtu`` are dropped while smaller packets still pass. Lab
# approximation uses an iptables OUTPUT length match (does not rewrite
# interface MTU or PMTUD sysctls). Legacy id: link_fragmentation_disabled.


class MtuMismatchParams(BaseModel):
    """Parameters for injecting a path-MTU / MTU-mismatch fault."""

    host_name: str = Field(description="Target host name.")
    mtu: int = Field(
        default=100,
        description=(
            "Effective MTU threshold in bytes. Packets with length >= mtu "
            "are dropped; smaller packets pass."
        ),
    )


class MtuMismatch(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    cause = FailureCause.CONFIGURATION
    symptom = FailureSymptom.LOSS
    scope = FailureScope.PATH
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.PARTIAL
    root_cause_name: str = "mtu_mismatch"
    TAGS: str = ["link"]

    Params = MtuMismatchParams

    symptom_desc = (
        "Users report size-dependent packet loss: large transfers fail while "
        "small packets (for example small pings) still succeed."
    )

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.mtu = 100

    def root_cause_resources(self, params: MtuMismatchParams):
        return [interface_on(self.net_env, params.host_name, "eth0")]

    def inject_fault(self, params: MtuMismatchParams):
        match self.lab_backend:
            case "kathara":
                self._inject_mtu_mismatch_kathara(params)
            case "containerlab":
                self._inject_mtu_mismatch_containerlab(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot inject_fault: unsupported backend {backend!r}."
                )

    def _inject_mtu_mismatch_kathara(self, params: MtuMismatchParams) -> None:
        self._inject_mtu_mismatch(params)

    def _inject_mtu_mismatch_containerlab(self, params: MtuMismatchParams) -> None:
        self._inject_mtu_mismatch(params)

    def _inject_mtu_mismatch(self, params: MtuMismatchParams) -> None:
        self.mtu = params.mtu
        self.runtime.exec(
            params.host_name,
            f"iptables -A OUTPUT -m length --length {int(params.mtu)}:65535 -j DROP",
        )
        system_logger.info(
            f"Injected MTU mismatch on {params.host_name} (drop length >={params.mtu})"
        )

    def verify_fault(self, params: MtuMismatchParams) -> dict:
        """Verify the length-based DROP rule that models the MTU threshold."""
        match self.lab_backend:
            case "kathara":
                return self._verify_mtu_mismatch_kathara(params)
            case "containerlab":
                return self._verify_mtu_mismatch_containerlab(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot verify_fault: unsupported backend {backend!r}."
                )

    def _verify_mtu_mismatch_kathara(self, params: MtuMismatchParams) -> dict:
        return self._verify_mtu_mismatch(params)

    def _verify_mtu_mismatch_containerlab(self, params: MtuMismatchParams) -> dict:
        return self._verify_mtu_mismatch(params)

    def _verify_mtu_mismatch(self, params: MtuMismatchParams) -> dict:
        rule_args = f"-m length --length {int(params.mtu)}:65535 -j DROP"
        verified = self.runtime.iptables_rule_present(
            params.host_name, "OUTPUT", rule_args
        )
        iptables_output = self.runtime.exec(
            params.host_name, "iptables -S OUTPUT"
        ).strip()
        expected_rule = f"-A OUTPUT {rule_args}"
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "mtu": params.mtu,
                "rule": expected_rule,
                "iptables_output": iptables_output,
            },
        )
