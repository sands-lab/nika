"""SDN controller / southbound failure implementations (ONOS)."""

from pydantic import BaseModel, Field

from nika.problems.root_cause import node_resource
from nika.problems.problem_base import (
    FailureDomain,
    ProblemBase,
    build_verify_result,
)
from nika.utils.logger import system_logger

logger = system_logger

ONOS_OF_PORT_DEFAULT = 6653
ONOS_OF_PORT_MISMATCH = 6633


class SDNControllerCrashParams(BaseModel):
    """Parameters for injecting an SDN controller crash fault."""

    host_name: str = Field(description="Target SDN controller host name.")


class SDNControllerCrash(ProblemBase):
    failure_domain = FailureDomain.MANAGEMENT_ORCHESTRATION_PLANE
    root_cause_name: str = "sdn_controller_crash"
    TAGS: str = ["sdn"]

    Params = SDNControllerCrashParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: SDNControllerCrashParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: SDNControllerCrashParams):
        # ONOS runs as a Java/karaf process; also match legacy POX if present.
        # nika/onos PID 1 is a keeper script, so killing Java leaves the
        # container exec-able for verify_fault.
        self.runtime.exec(
            params.host_name,
            "pkill -f 'onos-service|org.apache.karaf|java.*onos' || "
            "pkill -f pox.py || true; "
            "sleep 1",
        )

    def verify_fault(self, params: SDNControllerCrashParams) -> dict:
        try:
            pgrep_output = self.runtime.exec(
                params.host_name,
                "pgrep -af 'onos-service|karaf|java.*onos|pox.py' 2>/dev/null "
                "| grep -v 'pgrep\\|bash\\|grep\\|onos-entrypoint\\|sleep infinity' "
                "| grep . || echo NONE",
            ).strip()
        except Exception as exc:  # noqa: BLE001
            # Container exited with the controller process (legacy images).
            return build_verify_result(
                fault_type=self.root_cause_name,
                verified=True,
                details={
                    "host": params.host_name,
                    "pgrep_output": f"exec_failed: {exc}",
                },
            )
        verified = pgrep_output == "NONE" or (
            "onos" not in pgrep_output
            and "karaf" not in pgrep_output
            and "pox" not in pgrep_output
            and "java" not in pgrep_output
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "pgrep_output": pgrep_output},
        )


class SouthboundPortBlockParams(BaseModel):
    """Parameters for injecting a southbound port block fault."""

    host_name: str = Field(description="Target SDN controller host name.")
    southbound_port: int = Field(
        default=ONOS_OF_PORT_DEFAULT, description="Port to block."
    )


class SouthboundPortBlock(ProblemBase):
    failure_domain = FailureDomain.MANAGEMENT_ORCHESTRATION_PLANE
    root_cause_name: str = "southbound_port_block"
    TAGS: str = ["sdn"]

    Params = SouthboundPortBlockParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: SouthboundPortBlockParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: SouthboundPortBlockParams):
        self.runtime.add_nft_drop_rule(
            params.host_name, f"tcp dport {params.southbound_port} drop"
        )

    def verify_fault(self, params: SouthboundPortBlockParams) -> dict:
        nft_output = self.runtime.exec(
            params.host_name, "nft list ruleset 2>/dev/null"
        ).strip()
        verified = (
            f"tcp dport {params.southbound_port}" in nft_output and "drop" in nft_output
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "nft_output": nft_output},
        )


class SouthboundPortMismatchParams(BaseModel):
    """Parameters for injecting a southbound port mismatch fault."""

    host_name: str = Field(description="Target SDN controller host name.")
    mismatched_port: int = Field(
        default=ONOS_OF_PORT_MISMATCH,
        description="Port used after reconfigure (switches keep original).",
    )
    original_port: int = Field(
        default=ONOS_OF_PORT_DEFAULT,
        description="Expected original OpenFlow port.",
    )


class SouthboundPortMismatch(ProblemBase):
    failure_domain = FailureDomain.MANAGEMENT_ORCHESTRATION_PLANE
    root_cause_name: str = "southbound_port_mismatch"
    TAGS: str = ["sdn"]

    Params = SouthboundPortMismatchParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: SouthboundPortMismatchParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: SouthboundPortMismatchParams):
        # Block the original OpenFlow port and listen on the mismatched port so
        # switches still targeting original_port fail to session while evidence
        # shows a listener on the wrong port.
        self.runtime.add_nft_drop_rule(
            params.host_name, f"tcp dport {params.original_port} drop"
        )
        self.runtime.exec(
            params.host_name,
            f'nohup python3 -c "import socket,time;s=socket.socket();'
            f"s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
            f"s.bind(('0.0.0.0',{params.mismatched_port}));s.listen(1);"
            f'time.sleep(3600)" >/tmp/of_mismatch.log 2>&1 &',
        )

    def verify_fault(self, params: SouthboundPortMismatchParams) -> dict:
        nft_output = self.runtime.exec(
            params.host_name, "nft list ruleset 2>/dev/null"
        ).strip()
        listen = self.runtime.exec(
            params.host_name,
            f"ss -lnt 2>/dev/null | grep ':{params.mismatched_port} ' || "
            f"netstat -lnt 2>/dev/null | grep ':{params.mismatched_port} ' || echo NONE",
        ).strip()
        blocked = (
            f"tcp dport {params.original_port}" in nft_output and "drop" in nft_output
        )
        listening = listen != "NONE" and str(params.mismatched_port) in listen
        verified = blocked and listening
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "nft_output": nft_output,
                "listen": listen,
            },
        )
