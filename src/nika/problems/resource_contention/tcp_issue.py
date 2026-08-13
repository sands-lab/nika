from nika.problems.root_cause import node_resource
from nika.problems.problem_base import (
    RootCauseCategory,
    build_verify_result,
    ProblemBase,
)
from nika.utils.logger import system_logger
from pydantic import BaseModel, Field

_STRESS_CMD = (
    "nohup stress-ng --cpu 0 --cpu-load 100 --iomix 0 --sock 0 --hdd 2 "
    "--vm 0 --vm-bytes 75% --timeout {duration} </dev/null >/dev/null 2>&1 &"
)

_SLOW_SENDER_SERVER = """\
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

CHUNK = b"x" * 1024
DELAY = 0.1

class SlowSenderHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()

        for _ in range(5 * 1024):
            time.sleep(DELAY)
            self.wfile.write(CHUNK)

server = HTTPServer(("", 80), SlowSenderHandler)
server.serve_forever()
"""

# ==================================================================
# Problem: sender resource contention. Ref. Dapper: Data Plane Performance Diagnosis of TCP
# ==================================================================


class SenderResourceContentionParams(BaseModel):
    """Parameters for injecting a sender resource contention fault."""

    host_name: str = Field(description="Target sender host name.")
    duration: int = Field(default=600, description="Stress duration in seconds.")


class SenderResourceContention(ProblemBase):
    root_cause_category: RootCauseCategory = RootCauseCategory.RESOURCE_CONTENTION
    root_cause_name: str = "sender_resource_contention"
    TAGS: str = ["http"]

    Params = SenderResourceContentionParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: SenderResourceContentionParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: SenderResourceContentionParams):
        self.runtime.exec(
            params.host_name, _STRESS_CMD.format(duration=params.duration)
        )
        system_logger.info(
            f"Injected TCP slow sender issue on params.host_name {params.host_name}"
        )

    def verify_fault(self, params: SenderResourceContentionParams) -> dict:
        """Verify stress-ng is running on the sender params.host_name."""
        verified = self.runtime.process_running(params.host_name, "stress-ng")
        return build_verify_result(
            root_cause_name=self.root_cause_name,
            faulty_devices=self.faulty_devices,
            verified=verified,
            details={"host": params.host_name},
        )


# ==================================================================
# Problem: Application level delay causing TCP sender issues
# ==================================================================


class SenderApplicationDelayParams(BaseModel):
    """Parameters for injecting a sender application delay fault."""

    host_name: str = Field(description="Target sender host name.")


class SenderApplicationDelay(ProblemBase):
    root_cause_category: RootCauseCategory = RootCauseCategory.RESOURCE_CONTENTION
    root_cause_name: str = "sender_application_delay"
    TAGS: str = ["http"]

    Params = SenderApplicationDelayParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: SenderApplicationDelayParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: SenderApplicationDelayParams):
        self.runtime.exec(params.host_name, "cp web_server.py web_server.py.bak")
        self.runtime.write_file(params.host_name, "/web_server.py", _SLOW_SENDER_SERVER)
        self.runtime.systemctl(params.host_name, "web_server.service", "restart")
        system_logger.info(
            f"Injected TCP sender application delay issue on params.host_name {params.host_name}"
        )

    def verify_fault(self, params: SenderApplicationDelayParams) -> dict:
        """Verify the web_server.py has a sleep call injected."""
        verified = self.runtime.file_contains(
            params.host_name, "/web_server.py", "time.sleep"
        )
        return build_verify_result(
            root_cause_name=self.root_cause_name,
            faulty_devices=self.faulty_devices,
            verified=verified,
            details={"host": params.host_name},
        )


# ==================================================================
# Problem: receiver resource contention
# ==================================================================


class ReceiverResourceContentionParams(BaseModel):
    """Parameters for injecting a receiver resource contention fault."""

    host_name: str = Field(description="Target receiver host name.")
    duration: int = Field(default=600, description="Stress duration in seconds.")


class ReceiverResourceContention(ProblemBase):
    root_cause_category: RootCauseCategory = RootCauseCategory.RESOURCE_CONTENTION
    root_cause_name: str = "receiver_resource_contention"
    TAGS: str = ["http"]

    Params = ReceiverResourceContentionParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: ReceiverResourceContentionParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: ReceiverResourceContentionParams):
        self.runtime.exec(
            params.host_name, _STRESS_CMD.format(duration=params.duration)
        )
        system_logger.info(
            f"Injected TCP receiver resource contention on params.host_name {params.host_name}"
        )

    def verify_fault(self, params: ReceiverResourceContentionParams) -> dict:
        """Verify stress-ng is running on the receiver params.host_name."""
        pgrep_output = self.runtime.exec(
            params.host_name, "pgrep -a stress-ng 2>/dev/null || echo NONE"
        ).strip()
        verified = "stress-ng" in pgrep_output and pgrep_output != "NONE"
        return build_verify_result(
            root_cause_name=self.root_cause_name,
            faulty_devices=self.faulty_devices,
            verified=verified,
            details={"host": params.host_name, "pgrep_output": pgrep_output},
        )
